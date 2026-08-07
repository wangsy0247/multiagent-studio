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

_SUMMARY_PROMPT = """你是历史会话检索助手。用户当前想查找："{query}"

下面是该用户一个历史会话的内容（围绕命中位置裁剪的对话记录）。
请总结其中与查找目标最相关的内容，保留关键细节（结论、代码、配置、
报错信息、具体取值等），不要泛泛而谈。若内容与查找目标无关，直接回答
"无相关内容"。用 3-8 句话，使用与用户相同的语言。

会话标题：{title}

对话记录：
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
        raise RuntimeError("会话搜索功能未启用（服务未配置 INTERNAL_API_TOKEN）")
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
        raise RuntimeError(f"会话搜索服务错误 ({resp.status_code}): {detail}")
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
                query=query, title=sess.get("title") or "(无标题)", transcript=transcript,
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
            f"## 会话: {s.get('title') or '(无标题)'}"
            f" (thread_id: {s['thread_id']}, 命中 {len(s.get('matches') or [])} 处)"
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
        """搜索用户的历史会话消息（全文关键词检索，支持中英文）。

        命中后由小模型总结会话中与 query 最相关的内容（结论、代码、配置、
        报错等细节）返回；总结不可用时会返回围绕命中裁剪的原始对话记录。

        使用场景: 用户引用之前讨论过的内容（"我们之前说的那个方案"、"上次那个报错"），
        或需要从历史会话中查找结论、代码、配置时。

        使用纪律（必须遵守）:
        - query 用关键词而非整句；中文可用 2 字以上词语，多个词用空格分隔（AND）
          或用 OR 连接（如 "广西 OR 桂林"）；英文短语可加引号（如 "docker compose"）
        - 只搜索历史会话，当前会话已被自动排除
        - 搜不到时换同义词/更短的关键词重试，不要反复用相同 query
        - 返回内容若带 "...[earlier/later conversation truncated]..." 标记，
          说明会话较长只保留了命中附近部分；可用更精确的关键词再搜定位其他部分

        Args:
            query: 搜索关键词（支持 AND/OR/NOT、引号短语、英文前缀 *）
            max_sessions: 最多返回几个会话（1-5，默认 3）
        """
        ctx = _extract_context(state, config)
        if not ctx["user_id"]:
            return "会话搜索不可用：无法确定当前用户。"
        try:
            data = await _call_app("/api/internal/session-search", {
                "username": ctx["user_id"],
                "query": query,
                "exclude_thread_id": ctx["thread_id"] or None,
                "max_sessions": max_sessions,
            })
        except RuntimeError as e:
            return f"会话搜索失败: {e}"
        sessions = data.get("sessions") or []
        if not sessions:
            return "未在历史会话中找到匹配的消息。可尝试更换关键词（更短、同义词或 OR 组合）。"
        summaries = await _summarize_all(sessions, query, ctx["user_id"])
        return _format_results(sessions, summaries)

    return session_search
