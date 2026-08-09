"""Usage ledger — per-call token usage records for all LLM invocations.

Design notes:

- Every LLM call (main agent, teammates, subagents, and side-channel calls
  such as title generation, summarization, and memory updates) produces one
  record in a single SQLite file under the data root.  This is the single
  source of truth for token statistics; SSE token events are only used for
  live bubble display and are not persisted.
- Call context (user_id / thread_id / run_id / source) travels through a
  ContextVar, following the same pattern as ``_current_req_creds`` in
  ``harness.main``.  Callers set it once at run entry; side-channel call
  sites override ``source`` with :func:`usage_context`.
- Records without an attributable ``user_id`` + ``thread_id`` are skipped —
  unattributable usage should not silently pollute statistics.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Iterator

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)

# Valid ``source`` values for usage records.
SOURCES = (
    "main",            # single-agent main loop
    "subagent",        # spawned sub-agents
    "team_member",     # team mode teammates (incl. lead orchestration)
    "team_synthesis",  # team lead final synthesis
    "title",           # thread title generation
    "summary",         # history summarization / compression
    "memory",          # memory updates (incl. member-memory rewrites)
)

# Side-channel run names — filtered out of live SSE token events so
# in-graph side calls do not pollute the per-turn bubble totals.
SIDE_CHANNEL_RUN_NAMES = {"title_gen", "summary_gen", "memory_agent"}

_usage_ctx: ContextVar[dict[str, Any]] = ContextVar("usage_ctx", default={})


def current_usage_context() -> dict[str, Any]:
    """Return a copy of the current usage context."""
    return dict(_usage_ctx.get())


def set_usage_context(fields: dict[str, Any]) -> Token:
    """Replace the usage context (returns a token for reset)."""
    return _usage_ctx.set(dict(fields))


def reset_usage_context(token: Token) -> None:
    _usage_ctx.reset(token)


@contextmanager
def usage_context(**overrides: Any) -> Iterator[None]:
    """Merge ``overrides`` into the current usage context for the block."""
    merged = {**_usage_ctx.get(), **overrides}
    token = _usage_ctx.set(merged)
    try:
        yield
    finally:
        _usage_ctx.reset(token)


def extract_usage(
    usage_metadata: dict[str, Any] | None,
    response_metadata: dict[str, Any] | None,
) -> dict[str, int]:
    """Normalize token usage across providers.

    Handles three shapes:

    1. LangChain ``usage_metadata`` (OpenAI standard): ``input_tokens`` /
       ``output_tokens`` / ``total_tokens`` + ``input_token_details.cache_read``
       (mapped from ``prompt_tokens_details.cached_tokens``).
    2. DeepSeek raw payload: ``prompt_cache_hit_tokens`` /
       ``prompt_cache_miss_tokens`` in ``response_metadata.token_usage``.
    3. Legacy fallback: ``response_metadata.token_usage`` OpenAI fields when
       ``usage_metadata`` is absent.

    ``cache_miss`` falls back to ``prompt_tokens - cache_hit`` when the
    provider does not report it explicitly.
    """
    um = usage_metadata or {}
    raw = (response_metadata or {}).get("token_usage") or {}

    prompt = int(um.get("input_tokens") or raw.get("prompt_tokens") or 0)
    completion = int(um.get("output_tokens") or raw.get("completion_tokens") or 0)
    total = int(um.get("total_tokens") or raw.get("total_tokens") or (prompt + completion))

    cache_hit = 0
    details = um.get("input_token_details") or {}
    if details.get("cache_read"):
        cache_hit = int(details["cache_read"])
    if not cache_hit:
        prompt_details = raw.get("prompt_tokens_details") or {}
        cache_hit = int(prompt_details.get("cached_tokens") or 0)
    if not cache_hit:
        cache_hit = int(raw.get("prompt_cache_hit_tokens") or 0)

    if "prompt_cache_miss_tokens" in raw:
        cache_miss = int(raw.get("prompt_cache_miss_tokens") or 0)
    else:
        cache_miss = max(prompt - cache_hit, 0)

    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cache_hit_tokens": cache_hit,
        "cache_miss_tokens": cache_miss,
    }


class UsageLedger:
    """SQLite-backed store of per-call usage records."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS usage_records (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts REAL NOT NULL,
      user_id TEXT NOT NULL,
      thread_id TEXT NOT NULL,
      run_id TEXT,
      source TEXT NOT NULL,
      model TEXT NOT NULL,
      prompt_tokens INTEGER NOT NULL DEFAULT 0,
      completion_tokens INTEGER NOT NULL DEFAULT 0,
      total_tokens INTEGER NOT NULL DEFAULT 0,
      cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
      cache_miss_tokens INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_records(user_id, ts);
    CREATE INDEX IF NOT EXISTS idx_usage_thread ON usage_records(thread_id);
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(self._SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def record(
        self,
        *,
        user_id: str,
        thread_id: str,
        source: str,
        model: str,
        run_id: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
        ts: float | None = None,
    ) -> None:
        """Insert one usage record.  Failures are logged, never raised."""
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO usage_records
                      (ts, user_id, thread_id, run_id, source, model,
                       prompt_tokens, completion_tokens, total_tokens,
                       cache_hit_tokens, cache_miss_tokens)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts if ts is not None else time.time(),
                        user_id, thread_id, run_id, source, model,
                        prompt_tokens, completion_tokens, total_tokens,
                        cache_hit_tokens, cache_miss_tokens,
                    ),
                )
        except Exception:
            logger.exception("UsageLedger.record failed")

    def aggregate(
        self,
        user_id: str,
        *,
        thread_id: str | None = None,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> dict[str, Any]:
        """Aggregate usage for a user, optionally scoped to a thread/window.

        Returns totals plus per-model / per-date / per-source breakdowns.
        """
        where = ["user_id = ?"]
        params: list[Any] = [user_id]
        if thread_id:
            where.append("thread_id = ?")
            params.append(thread_id)
        if start_ts is not None:
            where.append("ts >= ?")
            params.append(start_ts)
        if end_ts is not None:
            where.append("ts <= ?")
            params.append(end_ts)
        clause = " AND ".join(where)

        sums = (
            "COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0),"
            " COALESCE(SUM(total_tokens),0), COALESCE(SUM(cache_hit_tokens),0),"
            " COALESCE(SUM(cache_miss_tokens),0)"
        )
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    f"SELECT {sums} FROM usage_records WHERE {clause}", params
                ).fetchone()
                by_model = conn.execute(
                    f"SELECT model, {sums} FROM usage_records WHERE {clause}"
                    " GROUP BY model ORDER BY 4 DESC",
                    params,
                ).fetchall()
                by_source = conn.execute(
                    f"SELECT source, {sums} FROM usage_records WHERE {clause}"
                    " GROUP BY source ORDER BY 4 DESC",
                    params,
                ).fetchall()
                by_date = conn.execute(
                    f"SELECT date(ts, 'unixepoch', 'localtime') AS d, {sums}"
                    f" FROM usage_records WHERE {clause} GROUP BY d ORDER BY d",
                    params,
                ).fetchall()
        except Exception:
            logger.exception("UsageLedger.aggregate failed")
            row = (0, 0, 0, 0, 0)
            by_model, by_source, by_date = [], [], []

        def _pack(r: tuple, key: str, val: Any) -> dict[str, Any]:
            return {
                key: val,
                "prompt_tokens": r[1] or 0,
                "completion_tokens": r[2] or 0,
                "total_tokens": r[3] or 0,
                "cache_hit_tokens": r[4] or 0,
                "cache_miss_tokens": r[5] or 0,
            }

        return {
            "prompt_tokens": row[0] or 0,
            "completion_tokens": row[1] or 0,
            "total_tokens": row[2] or 0,
            "cache_hit_tokens": row[3] or 0,
            "cache_miss_tokens": row[4] or 0,
            "by_model": [_pack(r, "model", r[0]) for r in by_model],
            "by_source": [_pack(r, "source", r[0]) for r in by_source],
            "by_date": [_pack(r, "date", r[0]) for r in by_date],
        }


# ── Singletons ────────────────────────────────────────────────────────────

_ledger: UsageLedger | None = None
_ledger_lock = threading.Lock()


def get_usage_ledger() -> UsageLedger:
    """Return the process-wide ledger (lives under the harness data root)."""
    global _ledger
    with _ledger_lock:
        if _ledger is None:
            from harness.config.paths import get_paths

            _ledger = UsageLedger(Path(get_paths().base_dir) / "usage_ledger.db")
        return _ledger


def reset_usage_ledger() -> None:
    """Drop the singleton (tests that relocate the data root)."""
    global _ledger
    with _ledger_lock:
        _ledger = None


class UsageLedgerCallback(BaseCallbackHandler):
    """LangChain callback that writes every LLM call into the usage ledger.

    Attach at ``ChatOpenAI`` construction (``callbacks=[...]``); call context
    is read from the ContextVar at call time, so a cached LLM instance still
    attributes each call correctly.
    """

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        try:
            message = None
            generations = getattr(response, "generations", None) or []
            if generations and generations[0]:
                message = getattr(generations[0][-1], "message", None)
            usage_meta = getattr(message, "usage_metadata", None) if message else None
            response_meta = getattr(message, "response_metadata", None) if message else None
            usage = extract_usage(usage_meta, response_meta)
            if not usage["total_tokens"]:
                return

            ctx = _usage_ctx.get()
            user_id = ctx.get("user_id")
            thread_id = ctx.get("thread_id")
            if not user_id or not thread_id:
                return

            model = ""
            llm_output = getattr(response, "llm_output", None) or {}
            model = llm_output.get("model_name") or ctx.get("model") or "unknown"

            get_usage_ledger().record(
                user_id=str(user_id),
                thread_id=str(thread_id),
                run_id=ctx.get("run_id"),
                source=str(ctx.get("source") or "main"),
                model=str(model),
                **usage,
            )
        except Exception:
            logger.exception("UsageLedgerCallback.on_llm_end failed")

    # Sync + async variants share the implementation.
    async def aon_llm_end(self, response: Any, **kwargs: Any) -> None:
        self.on_llm_end(response, **kwargs)

_callback: UsageLedgerCallback | None = None


def get_usage_ledger_callback() -> UsageLedgerCallback:
    """Return a shared callback instance for LLM constructors."""
    global _callback
    if _callback is None:
        _callback = UsageLedgerCallback()
    return _callback
