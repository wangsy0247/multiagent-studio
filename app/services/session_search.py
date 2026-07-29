"""
session_search 服务 — 基于 SQLite FTS5 的历史会话消息全文搜索

三路由分流（移植自 hermes-agent `hermes_state.py:search_messages`）：
- 路由 1 非 CJK 查询      → messages_fts（unicode61），BM25 排序 + snippet 高亮
- 路由 2 CJK 且每 token ≥3 字 → messages_fts_trigram（trigram 需 ≥9 UTF-8 字节才能命中）
- 路由 3 CJK 短词/混合     → LIKE 全表扫描兜底（trigram 对 <3 字 token 无能为力，hermes #20494）

搜索结果限定当前用户的未归档会话，并按会话分组返回 Top N。
每个会话除命中证据（小 snippet）外，还返回围绕命中位置裁剪的完整对话
transcript（对齐 hermes 的 _truncate_around_matches），agent 需要细节
（完整代码/方案/报错）时可直接拿到。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# 每个会话最多保留的命中条数（避免单会话刷屏）
_MAX_MATCHES_PER_SESSION = 5

# 每个会话返回的对话全文上限（字符）。hermes 用 ~100k 喂辅助 LLM 做总结，
# 我们直接返回给调用方 LLM，取更保守的预算（×3 会话约 36k 字符）
_MAX_SESSION_CHARS = 12_000

_OPERATORS = {"AND", "OR", "NOT"}


# ── FTS5 查询清洗（移植自 hermes _sanitize_fts5_query）──────────────────────


def _sanitize_fts5_query(query: str) -> str:
    """清洗用户输入，使其可安全用于 FTS5 MATCH。

    - 保留配对引号包裹的精确短语
    - 剥离会导致语法错误的特殊字符
    - 连字符/点号/下划线连接的术语加引号，避免被分词器拆开
    - 去掉悬空的 AND/OR/NOT
    """
    quoted_parts: list[str] = []

    def _preserve_quoted(m: re.Match) -> str:
        quoted_parts.append(m.group(0))
        return f"\x00Q{len(quoted_parts) - 1}\x00"

    sanitized = re.sub(r'"[^"]*"', _preserve_quoted, query)
    sanitized = re.sub(r'[+{}()\"^]', " ", sanitized)
    sanitized = re.sub(r"\*+", "*", sanitized)
    sanitized = re.sub(r"(^|\s)\*", r"\1", sanitized)
    sanitized = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", sanitized.strip())
    sanitized = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", sanitized.strip())
    sanitized = re.sub(r"\b(\w+(?:[._-]\w+)+)\b", r'"\1"', sanitized)
    for i, quoted in enumerate(quoted_parts):
        sanitized = sanitized.replace(f"\x00Q{i}\x00", quoted)
    return sanitized.strip()


# ── CJK 判定（移植自 hermes）────────────────────────────────────────────────


def _is_cjk_codepoint(cp: int) -> bool:
    return (0x4E00 <= cp <= 0x9FFF or      # CJK Unified Ideographs
            0x3400 <= cp <= 0x4DBF or      # CJK Extension A
            0x20000 <= cp <= 0x2A6DF or    # CJK Extension B
            0x3000 <= cp <= 0x303F or      # CJK Symbols
            0x3040 <= cp <= 0x309F or      # Hiragana
            0x30A0 <= cp <= 0x30FF or      # Katakana
            0xAC00 <= cp <= 0xD7AF)        # Hangul Syllables


def _contains_cjk(text_: str) -> bool:
    return any(_is_cjk_codepoint(ord(ch)) for ch in text_)


def _count_cjk(text_: str) -> int:
    return sum(1 for ch in text_ if _is_cjk_codepoint(ord(ch)))


# ── 主入口 ──────────────────────────────────────────────────────────────────


async def search_messages(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    query: str,
    exclude_thread_id: str | None = None,
    max_sessions: int = 3,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """按关键词搜索某用户的历史会话消息，按会话分组返回。

    返回 [{thread_id, title, matches: [{role, msg_type, snippet, created_at, context}]}]，
    会话顺序按命中相关度（首条命中的排名）。
    """
    if not query or not query.strip():
        return []

    query = _sanitize_fts5_query(query)
    if not query:
        return []

    uid = user_id.hex if isinstance(user_id, uuid.UUID) else str(user_id).replace("-", "")
    excl = exclude_thread_id.replace("-", "") if exclude_thread_id else None
    max_sessions = max(1, min(5, max_sessions))

    scope_sql = "t.user_id = :uid AND t.is_archived = 0"
    params: dict[str, Any] = {"uid": uid, "limit": limit}
    if excl:
        scope_sql += " AND m.thread_id != :excl"
        params["excl"] = excl

    matches: list[dict[str, Any]] = []

    if not _contains_cjk(query):
        # 路由 1：unicode61 FTS5
        params["q"] = query
        sql = f"""
            SELECT m.thread_id, m.role, m.msg_type,
                   snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                   m.created_at, t.title
            FROM messages_fts
            JOIN messages m ON m.rowid = messages_fts.rowid
            JOIN threads t ON t.id = m.thread_id
            WHERE messages_fts MATCH :q AND {scope_sql}
            ORDER BY rank
            LIMIT :limit
        """
        try:
            rows = (await db.execute(text(sql), params)).mappings().all()
        except Exception:
            # 清洗后仍可能出现 FTS5 语法错误 —— 按空结果处理
            rows = []
        matches = [dict(r) for r in rows]
    else:
        raw_query = query.strip('"').strip()
        cjk_count = _count_cjk(raw_query)
        tokens_for_check = [
            t for t in raw_query.split()
            if t.upper() not in _OPERATORS and _contains_cjk(t)
        ]
        any_short_cjk = any(_count_cjk(t) < 3 for t in tokens_for_check)

        if cjk_count >= 3 and not any_short_cjk:
            # 路由 2：trigram FTS5（非运算符 token 加引号，保留布尔运算符）
            parts = [
                tok if tok.upper() in _OPERATORS
                else '"' + tok.replace('"', '""') + '"'
                for tok in raw_query.split()
            ]
            params["q"] = " ".join(parts)
            sql = f"""
                SELECT m.thread_id, m.role, m.msg_type,
                       snippet(messages_fts_trigram, 0, '>>>', '<<<', '...', 40) AS snippet,
                       m.created_at, t.title
                FROM messages_fts_trigram
                JOIN messages m ON m.rowid = messages_fts_trigram.rowid
                JOIN threads t ON t.id = m.thread_id
                WHERE messages_fts_trigram MATCH :q AND {scope_sql}
                ORDER BY rank
                LIMIT :limit
            """
            try:
                rows = (await db.execute(text(sql), params)).mappings().all()
            except Exception:
                rows = []
            matches = [dict(r) for r in rows]
        else:
            # 路由 3：短 CJK 词 LIKE 兜底（每个非运算符 token 一个独立条件，OR 连接）。
            # 与 FTS 索引口径一致：content + extra_metadata 全部 JSON 文本值
            # （json_tree 还原 \uXXXX 转义，中文工具参数可命中）
            non_op_tokens = [
                t for t in raw_query.split() if t.upper() not in _OPERATORS
            ] or [raw_query]
            token_clauses = []
            for i, tok in enumerate(non_op_tokens):
                esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                token_clauses.append(
                    f"(m.content LIKE :tok{i} ESCAPE '\\'"
                    f" OR EXISTS (SELECT 1 FROM json_tree(m.extra_metadata) jt"
                    f" WHERE jt.type = 'text' AND jt.value LIKE :tok{i} ESCAPE '\\'))"
                )
                params[f"tok{i}"] = f"%{esc}%"
            like_sql = " OR ".join(token_clauses)
            params["first_tok"] = non_op_tokens[0]
            sql = f"""
                SELECT m.thread_id, m.role, m.msg_type,
                       substr(m.content, max(1, instr(m.content, :first_tok) - 40), 120) AS snippet,
                       m.created_at, t.title
                FROM messages m
                JOIN threads t ON t.id = m.thread_id
                WHERE ({like_sql}) AND {scope_sql}
                ORDER BY m.created_at DESC
                LIMIT :limit
            """
            rows = (await db.execute(text(sql), params)).mappings().all()
            matches = [dict(r) for r in rows]

    # 按会话分组，保序取 Top N
    sessions: dict[str, dict[str, Any]] = {}
    for m in matches:
        tid = str(m["thread_id"])
        if tid not in sessions:
            if len(sessions) >= max_sessions:
                continue
            sessions[tid] = {
                "thread_id": tid,
                "title": m["title"],
                "matches": [],
            }
        bucket = sessions[tid]["matches"]
        if len(bucket) < _MAX_MATCHES_PER_SESSION:
            bucket.append({
                "role": m["role"],
                "msg_type": m["msg_type"],
                "snippet": m["snippet"],
                "created_at": str(m["created_at"]),
            })

    # 对齐 hermes：拉每个命中会话的完整对话，围绕命中位置裁剪后返回全文，
    # 而不是只给小 snippet —— agent 需要细节（完整代码/方案）时拿得到
    for sess in sessions.values():
        sess["transcript"] = await _fetch_transcript(db, sess["thread_id"], query)

    return list(sessions.values())


# ── 会话全文拉取与裁剪（移植自 hermes session_search_tool）───────────────────


async def _fetch_transcript(
    db: AsyncSession, thread_id: str, query: str, max_chars: int = _MAX_SESSION_CHARS
) -> str:
    """拉取整会话消息 → 格式化为对话文本 → 围绕命中位置裁剪到 max_chars。"""
    sql = """
        SELECT role, msg_type, content, extra_metadata
        FROM messages WHERE thread_id = :tid ORDER BY rowid
    """
    try:
        rows = (await db.execute(text(sql), {"tid": thread_id})).mappings().all()
    except Exception:
        return ""
    full_text = _format_conversation([dict(r) for r in rows])
    return _truncate_around_matches(full_text, query, max_chars)


def _format_conversation(messages: list[dict[str, Any]]) -> str:
    """格式化为可读对话文本（对齐 hermes，适配本项目 role/msg_type）。"""
    parts = []
    for msg in messages:
        role = (msg.get("role") or "unknown").upper()
        content = msg.get("content") or ""
        meta = msg.get("extra_metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        tool_name = meta.get("tool_name")

        if msg.get("msg_type") in ("tool_call", "tool_result"):
            # tool_call 的 content 为空，参数在 extra_metadata 里
            if not content and meta.get("tool_args"):
                content = json.dumps(meta["tool_args"], ensure_ascii=False)
            if len(content) > 500:
                content = content[:250] + "\n...[truncated]...\n" + content[-250:]
            label = f"TOOL:{tool_name}" if tool_name else "TOOL"
            parts.append(f"[{label}]: {content}")
        else:
            parts.append(f"[{role}]: {content}")
    return "\n\n".join(parts)


def _truncate_around_matches(
    full_text: str, query: str, max_chars: int = _MAX_SESSION_CHARS
) -> str:
    """把对话文本裁剪到 max_chars，选择最大化覆盖 query 命中位置的窗口。

    策略（按优先级，移植自 hermes）：
    1. 整句短语匹配（大小写不敏感）
    2. 所有词项在 200 字符邻近窗口内共现
    3. 单词项位置兜底
    选定候选位置后，取覆盖最多命中点的窗口（25% 在前、75% 在后）。
    """
    if len(full_text) <= max_chars:
        return full_text

    text_lower = full_text.lower()
    # 清洗 query：去引号、去布尔运算符，避免把 OR 当成搜索词
    cleaned = query.replace('"', " ")
    terms = [t for t in cleaned.split() if t.upper() not in _OPERATORS]
    query_lower = " ".join(terms).strip() or cleaned.strip()

    # 1. 整句短语
    match_positions: list[int] = []
    if query_lower:
        match_positions = [
            m.start() for m in re.finditer(re.escape(query_lower), text_lower)
        ]

    # 2. 全部词项 200 字符邻近共现
    if not match_positions and len(terms) > 1:
        term_positions = {
            t: [m.start() for m in re.finditer(re.escape(t.lower()), text_lower)]
            for t in terms
        }
        rarest = min(terms, key=lambda t: len(term_positions.get(t.lower(), term_positions.get(t, []))))
        rarest_positions = term_positions.get(rarest.lower(), term_positions.get(rarest, []))
        for pos in rarest_positions:
            if all(
                any(abs(p - pos) < 200 for p in term_positions.get(t.lower(), term_positions.get(t, [])))
                for t in terms
                if t != rarest
            ):
                match_positions.append(pos)

    # 3. 单词项位置兜底
    if not match_positions:
        for t in terms:
            for m in re.finditer(re.escape(t.lower()), text_lower):
                match_positions.append(m.start())

    if not match_positions:
        truncated = full_text[:max_chars]
        return truncated + "\n\n...[later conversation truncated]..."

    # 选覆盖最多命中点的窗口
    match_positions.sort()
    best_start, best_count = 0, 0
    for candidate in match_positions:
        ws = max(0, candidate - max_chars // 4)
        we = ws + max_chars
        if we > len(full_text):
            ws = max(0, len(full_text) - max_chars)
            we = len(full_text)
        count = sum(1 for p in match_positions if ws <= p < we)
        if count > best_count:
            best_count = count
            best_start = ws

    start = best_start
    end = min(len(full_text), start + max_chars)
    truncated = full_text[start:end]
    prefix = "...[earlier conversation truncated]...\n\n" if start > 0 else ""
    suffix = "\n\n...[later conversation truncated]..." if end < len(full_text) else ""
    return prefix + truncated + suffix
