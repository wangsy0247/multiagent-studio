"""Teammate 中间件构建器 — 17 层完整链.

与 Lead Agent 的 20 层链一致, 但排除:
  - ClarificationMiddleware  (teammate 不与用户交互)
  - TitleMiddleware           (仅 Lead 通过 keep_title=True 启用)

注册顺序与 AGENT_MIDDLEWARE_ORDER 对齐:
  [0-2]  Sandbox infrastructure
  [3-4]  wrap_model_call: Dangling + LLMError
  [5]    Guardrail
  [6]    SandboxAudit
  [7]    ToolErrorHandling
  [7.5]  DynamicContext
  [8]    Summarization
  [9]    Todo (Plan Mode)
  [10]   TokenUsage
  [11]   Memory
  [12]   ViewImage
  [13]   DeferredToolFilter
  [14]   SubagentLimit
  [15]   LoopDetection
  [16]   SafetyFinishReason
  [17]   Clarification (仅 Lead)
  [18]   Title (仅 Lead)
  [+ custom: InboxDrain 等]
"""

from __future__ import annotations

from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware

from harness.middleware.dangling_tool_call import DanglingToolCallMiddleware
from harness.middleware.llm_error import LLMErrorHandlingMiddleware
from harness.middleware.safety_finish_reason import SafetyFinishReasonMiddleware
from harness.middleware.sandbox_audit import SandboxAuditMiddleware
from harness.middleware.thread_data import ThreadDataMiddleware
from harness.middleware.tool_error import ToolErrorHandlingMiddleware
from harness.middleware.sandbox import SandboxMiddleware
from harness.middleware.uploads import UploadsMiddleware
from harness.middleware.guardrail import GuardrailMiddleware
from harness.middleware.todo import TodoMiddleware
from harness.middleware.token_usage import TokenUsageMiddleware
from harness.middleware.memory import MemoryMiddleware
from harness.middleware.view_image import ViewImageMiddleware
from harness.middleware.deferred_tool_filter import DeferredToolFilterMiddleware
from harness.middleware.subagent_limit import SubagentLimitMiddleware
from harness.middleware.loop_detection import LoopDetectionMiddleware

from harness.middleware.dynamic_context import DynamicContextMiddleware

try:
    from harness.middleware.summarization import create_summarization_middleware as _create_summarization
    _has_summarization = True
except ImportError:
    _has_summarization = False


def build_teammate_middlewares(
    *,
    workspace_root: str = "",
    agent_name: str | None = None,
    # 特性开关
    is_plan_mode: bool = False,
    subagent_enabled: bool = False,
    max_concurrent_subagents: int = 3,
    memory_enabled: bool = True,
    summarization_enabled: bool = False,
    guardrail_enabled: bool = False,
    vision_enabled: bool = False,
    tool_search_enabled: bool = False,
    tool_max_retries: int = 3,
    # Lead 专属
    keep_clarification: bool = False,
    keep_title: bool = False,  # 仅 Lead 生成对话标题
    # loop_detection 配置
    loop_cfg: dict[str, Any] | None = None,
    # 自定义中间件 (InboxDrain 等)
    custom_middlewares: list[AgentMiddleware] | None = None,
    # 模型配置
    summary_model: str = "",
    memory_model: str = "",
    api_key: str = "",
    base_url: str = "",
    user_id: str = "",
    # TitleMiddleware 配置 (仅 Lead)
    title_model: str = "gpt-4o-mini",
    title_emitted_ref: list | None = None,  # mutable [bool] 去重标志
    on_title: Callable | None = None,      # async callable(title: str)
    # ── 项目记忆 (Team 模式) ──
    project_context: str = "",
) -> list[AgentMiddleware]:
    """构建 Teammate 完整中间件链 (17-18 层).

    Lead 专属 (keep_title=True/keep_clarification=True):
      - TitleMiddleware (生成对话标题, 仅首次)
      - ClarificationMiddleware (向用户提问澄清)
    Member 专属:
      - SubagentLimitMiddleware (可委派子任务)
    """
    loop_cfg = loop_cfg or {}
    middlewares: list[AgentMiddleware] = []

    # [0-2] Sandbox infrastructure
    middlewares.append(ThreadDataMiddleware({"workspace_root": workspace_root}))
    middlewares.append(UploadsMiddleware())
    middlewares.append(SandboxMiddleware())

    # [3-4] wrap_model_call innermost
    middlewares.append(DanglingToolCallMiddleware())
    middlewares.append(LLMErrorHandlingMiddleware())

    # [5] Guardrail
    if guardrail_enabled:
        middlewares.append(GuardrailMiddleware())

    # [6] SandboxAudit
    middlewares.append(SandboxAuditMiddleware())

    # [7] ToolErrorHandling
    middlewares.append(ToolErrorHandlingMiddleware({"max_retries": tool_max_retries}))

    # [7.5] DynamicContext — 所有 teammate (Lead + Member) 注入记忆 + 日期 + 项目上下文
    middlewares.append(DynamicContextMiddleware(
        agent_name=agent_name, project_context=project_context or None,
    ))

    # [8] Summarization (长运行 teammate 需要上下文压缩)
    # 不挂 memory_flush_hook: MemoryMiddleware 每轮已增量提交最新交换.
    if summarization_enabled and _has_summarization:
        summ_mw = _create_summarization(
            model_name=summary_model,
            api_key=api_key,
            base_url=base_url,
            user_id=user_id,
        )
        if summ_mw is not None:
            middlewares.append(summ_mw)

    # [9] Todo (Plan Mode)
    if is_plan_mode:
        middlewares.append(TodoMiddleware())

    # [10] TokenUsage
    middlewares.append(TokenUsageMiddleware())

    # [11] Memory (teammate 有自己的记忆)
    if memory_enabled:
        middlewares.append(MemoryMiddleware(
            {"openai_api_key": api_key, "openai_base_url": base_url,
             "memory_model": memory_model},
            agent_name=agent_name,
        ))

    # [12] ViewImage
    if vision_enabled:
        middlewares.append(ViewImageMiddleware())

    # [13] DeferredToolFilter
    if tool_search_enabled:
        middlewares.append(DeferredToolFilterMiddleware())

    # [14] SubagentLimit
    if subagent_enabled:
        middlewares.append(SubagentLimitMiddleware({"max_concurrent": max_concurrent_subagents}))

    # [15] LoopDetection
    middlewares.append(LoopDetectionMiddleware(
        loop_cfg,
        warn_threshold=loop_cfg.get("warn_threshold", 7),
        hard_limit=loop_cfg.get("hard_limit", 10),
        tool_freq_warn=loop_cfg.get("tool_freq_warn", 30),
        tool_freq_hard_limit=loop_cfg.get("tool_freq_hard_limit", 50),
        window_size=loop_cfg.get("window_size", 20),
        # 豁免轮询等待类工具 (member 等 Lead 审批时会自然反复 read_inbox)
        exempt_tools=loop_cfg.get("exempt_tools", {"read_inbox"}),
    ))

    # [16] SafetyFinishReason
    middlewares.append(SafetyFinishReasonMiddleware())

    # [17] Clarification (仅 Lead — 需要向用户提问澄清)
    if keep_clarification:
        from harness.middleware.clarification import ClarificationMiddleware
        middlewares.append(ClarificationMiddleware())

    # [18] Title (仅 Lead — 首次回复后生成对话标题)
    if keep_title:
        from harness.middleware.title import TitleMiddleware
        title_config: dict[str, Any] = {
            "title_model": title_model,
            "api_key": api_key,
            "base_url": base_url,
            "user_id": user_id,
        }
        if title_emitted_ref is not None:
            title_config["_title_emitted_ref"] = title_emitted_ref
        if on_title is not None:
            title_config["on_title"] = on_title
        middlewares.append(TitleMiddleware(title_config))

    # [+] 自定义中间件 (InboxDrainMiddleware 等)
    if custom_middlewares:
        middlewares.extend(custom_middlewares)

    return middlewares
