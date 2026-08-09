"""Built-in ``session_search`` tool — Lead Agent 搜索用户的历史会话消息.

设计参照 hermes-agent 的 session_search 工具：
- 数据在 App 侧 app.db（messages 表 + FTS5 索引），harness 不直连业务库
- 走 App 内部接口 POST /api/internal/session-search（X-Internal-Token 认证），
  与 cron 工具同一模式
- 身份自动捕获：user_id 取自 InjectedState，thread_id 取自 RunnableConfig
  （作为 exclude_thread_id，避免搜到当前会话）
- App 返回围绕命中裁剪的会话全文 transcript 后，用小模型做聚焦 query 的
  并行总结（信号量限并发，对齐 hermes）；LLM 不可用/失败时降级为原始 transcript
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Annotated

import httpx
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import InjectedState

from harness.middleware.title import _strip_think_tags

logger = logging.getLogger(__name__)

APP_SERVICE_URL = os.getenv("APP_SERVICE_URL", "http://localhost:8000")
_HTTP_TIMEOUT = 30.0

# 并行总结的最大并发（对齐 hermes auxiliary.session_search.max_concurrency 默认值）
_SUMMARY_MAX_CONCURRENCY = 3

_SUMMARY_PROMPT = """You are a history-session retrieval assistant. The user is currently looking for: "{query}"

Below is the content of one of the user's past sessions (a transcript cropped around the matching positions).
Summarize the parts most relevant to what the user is looking for, keeping key details (conclusions, code,
configuration, error messages, specific values, etc.) — do not be generic. If the content is unrelated to the
search target, answer exactly "No relevant content". Use 3-8 sentences, in the same language as the user.

Session title: {title}

Transcript:
{transcript}"""


def _extract_context(state: dict | None, config: RunnableConfig | None) -> dict[str, str]:
    """从注入的 graph state / RunnableConfig 提取用户身份与当前会话 id。"""
    user_id = (state or {}).get("user_id") or ""
    thread_id = ""
    if config:
        thread_id = (config.get("configurable") or {}).get("thread_id", "") or ""
    return {"user_id": user_id, "thread_id": thread_id}


async def _call_app(path: str, payload: dict) -> dict:
    token = os.getenv("INTERNAL_API_TOKEN", "")
    if not token:
        raise RuntimeError("Session search is not enabled (INTERNAL_API_TOKEN not configured on the server)")
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{APP_SERVICE_URL}{path}",
            json=payload,
            headers={"X-Internal-Token": token},
        )
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text[:200])
        except Exception:
            detail = resp.text[:200]
        raise RuntimeError(f"Session search service error ({resp.status_code}): {detail}")
    return resp.json()


# ── 小模型总结（对齐 hermes 的辅助 LLM 摘要，凭证解析仿 TitleMiddleware）──────

_llm: ChatOpenAI | None = None
_llm_key: tuple[str, str, str] | None = None


def _resolve_summary_config(user_id: str) -> tuple[str, str, str]:
    """解析 (api_key, base_url, model)。

    凭证：请求级 contextvar（EffectiveConfig 服务器注入）→ 环境变量。
    模型：SESSION_SEARCH_MODEL → SUMMARY_MODEL/TITLE_MODEL 环境变量
    → 请求级模型 → gpt-4o-mini。
    """
    creds: dict = {}
    try:
        from harness.main import _current_req_creds  # 延迟导入避免循环依赖
        creds = _current_req_creds.get() or {}
    except Exception:
        pass

    api_key = creds.get("api_key") or os.getenv("OPENAI_API_KEY", "")
    base_url = (
        creds.get("base_url")
        or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    )
    model = (
        os.getenv("SESSION_SEARCH_MODEL", "")
        or os.getenv("SUMMARY_MODEL", "") or os.getenv("TITLE_MODEL", "")
        or creds.get("model")
        or "gpt-4o-mini"
    )
    return api_key, base_url, model


def _get_summary_llm(api_key: str, base_url: str, model: str) -> ChatOpenAI:
    """缓存 ChatOpenAI 实例（连接池复用），配置变化时重建。"""
    global _llm, _llm_key
    key = (api_key, base_url, model)
    if _llm is None or _llm_key != key:
        _llm = ChatOpenAI(
            model=model,
            temperature=0.2,
            api_key=api_key,
            base_url=base_url,
            request_timeout=30,
            max_retries=1,
            extra_body={
                "enable_thinking": False,           # DashScope / 通义千问
                "thinking": {"type": "disabled"},    # DeepSeek / Anthropic / Claude
            },
        )
        _llm_key = key
    return _llm


async def _summarize_all(
    sessions: list[dict], query: str, user_id: str
) -> list[str | None]:
    """并行总结各会话 transcript，返回与 sessions 对齐的摘要列表（None = 不可用/失败）。

    信号量限并发；单会话失败不拖垮整体（该会话降级为原始 transcript）。
    """
    if not any(s.get("transcript") for s in sessions):
        return [None] * len(sessions)
    api_key, base_url, model = _resolve_summary_config(user_id)
    if not api_key:
        logger.info("session_search: 无可用 LLM 凭证，跳过总结，返回原始 transcript")
        return [None] * len(sessions)

    llm = _get_summary_llm(api_key, base_url, model)
    semaphore = asyncio.Semaphore(min(_SUMMARY_MAX_CONCURRENCY, max(1, len(sessions))))

    async def _one(sess: dict) -> str | None:
        transcript = sess.get("transcript") or ""
        if not transcript:
            return None
        async with semaphore:
            prompt = _SUMMARY_PROMPT.format(
                query=query, title=sess.get("title") or "(untitled)", transcript=transcript,
            )
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            return _strip_think_tags(str(response.content)).strip()

    async def _guarded(sess: dict) -> str | None:
        try:
            return await _one(sess)
        except Exception as e:
            logger.warning("session_search 总结失败（降级为原始 transcript）: %s", e)
            return None

    return await asyncio.gather(*[_guarded(s) for s in sessions])


def _format_results(sessions: list[dict], summaries: list[str | None] | None = None) -> str:
    """格式化输出：优先小模型总结，无总结时给原始 transcript。"""
    blocks = []
    for i, s in enumerate(sessions):
        header = (
            f"## Session: {s.get('title') or '(untitled)'}"
            f" (thread_id: {s['thread_id']}, {len(s.get('matches') or [])} match(es))"
        )
        summary = summaries[i] if summaries and i < len(summaries) else None
        body = summary or s.get("transcript") or ""
        blocks.append(header + "\n\n" + body)
    return "\n\n---\n\n".join(blocks)


def create_session_search_tool() -> BaseTool:
    """Create the ``session_search`` tool used by the Lead Agent."""

    @tool
    async def session_search(
        query: str,
        max_sessions: int = 3,
        config: RunnableConfig = None,  # auto-injected by LangChain at call time
        state: Annotated[dict, InjectedState] = None,  # graph state (user_id)
    ) -> str:
        """Search the user's historical session messages (full-text keyword search, supports Chinese and English).

        On a hit, a small model summarizes the content most relevant to the query (conclusions, code,
        configuration, error details, etc.) and returns it; when summarization is unavailable, the raw
        transcript cropped around the matches is returned instead.

        Use cases: the user references something discussed before ("the plan we talked about earlier",
        "that error from last time"), or you need to find conclusions, code, or configuration from past sessions.

        Usage discipline (must follow):
        - Use keywords rather than full sentences for query; for Chinese use words of 2+ characters,
          separate multiple words with spaces (AND) or join them with OR (e.g. "广西 OR 桂林");
          English phrases can be quoted (e.g. "docker compose")
        - Only historical sessions are searched; the current session is automatically excluded
        - When nothing is found, retry with synonyms or shorter keywords; do not repeat the same query
        - If returned content carries a "...[earlier/later conversation truncated]..." marker,
          the session is long and only the parts near the matches were kept; search again with more
          precise keywords to locate other parts

        Args:
            query: search keywords (supports AND/OR/NOT, quoted phrases, English prefix *)
            max_sessions: maximum number of sessions to return (1-5, default 3)
        """
        ctx = _extract_context(state, config)
        if not ctx["user_id"]:
            return "Session search unavailable: cannot determine the current user."
        try:
            data = await _call_app("/api/internal/session-search", {
                "username": ctx["user_id"],
                "query": query,
                "exclude_thread_id": ctx["thread_id"] or None,
                "max_sessions": max_sessions,
            })
        except RuntimeError as e:
            return f"Session search failed: {e}"
        sessions = data.get("sessions") or []
        if not sessions:
            return "No matching messages found in historical sessions. Try different keywords (shorter, synonyms, or OR combinations)."
        summaries = await _summarize_all(sessions, query, ctx["user_id"])
        return _format_results(sessions, summaries)

    return session_search
