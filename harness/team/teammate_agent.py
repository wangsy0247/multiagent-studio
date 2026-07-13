"""TeammateAgent — 持久化 Teammate, 拥有自己的 agent loop.

参考 learn-claude-code  _teammate_loop 设计:
- 独立 asyncio Task 持续运行
- WORKING 阶段: 完整 ReAct agent loop, 多轮 LLM 推理
- IDLE 阶段: 事件驱动等待 (消息 / 新任务), 不再 sleep() 轮询
- 完成后回到 IDLE, 不销毁 — 跨任务保持上下文
- shutdown_request/plan_approval 协议消息处理
- _maybe_claim_task() 自主认领预留

设计: _agent_loop() 持续运行 → IDLE → 被唤醒 → WORKING → IDLE → ...
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool

from harness.config.agents_config import load_agent_config, load_agent_soul
from harness.team.context import TeamContext
from harness.team.message_bus import TeamMessageBus
from harness.team.models import (
    TeamMemberRuntime,
    TeamMessage,
    TeamMessageType,
    TeamTask,
    TeamTaskStatus,
    TeammateStatus,
    RequestStatus,
)
from harness.team.task_store import TeamTaskStore

logger = logging.getLogger(__name__)

# ── 常量 ──
IDLE_POLL_INTERVAL = 5.0       # IDLE 时 inbox 检查间隔 (秒)
MAX_WORK_TURNS = 50            # WORKING 阶段最大 LLM 轮次


class TeammateAgent:
    """持久化 Teammate Agent — 拥有自己的 agent loop + SOUL + 工具 + 记忆.

    每个 teammate 是独立的 asyncio Task, 在后台持续运行:
    - 用自己的 SOUL.md 作为 system prompt
    - 有独立的工具集和长期记忆
    - IDLE 时事件驱动等待, WORKING 时完整 ReAct 循环
    - 支持 spawn → WORKING → IDLE → SHUTDOWN 生命周期
    """

    def __init__(
        self,
        agent_name: str,
        llm: BaseChatModel,
        tools: list[BaseTool],
        team_context: TeamContext,
        message_bus: TeamMessageBus,
        task_store: TeamTaskStore,
        *,
        skill_storage: Any | None = None,
        event_queue: asyncio.Queue | None = None,
        role: str = "member",
        lead_name: str | None = None,
        tracer: Any = None,
    ) -> None:
        self.name = agent_name
        self.llm = llm
        self._tools = tools
        self._team_context = team_context
        self._message_bus = message_bus
        self._task_store = task_store
        self._skill_storage = skill_storage
        self._event_queue = event_queue  # SSE 事件输出 (orchestrator 注入)
        self._role = role  # "lead" | "member"
        self._lead_name = lead_name  # Lead 的 agent name (用于发 summary)
        self._tracer = tracer  # TeamTracer (可选, 用于 Langfuse 可视化)

        # ── 加载 Agent 配置和 SOUL ──
        user_id = team_context.user_id
        self._agent_config = load_agent_config(agent_name, user_id=user_id)
        self._agent_soul = load_agent_soul(agent_name, user_id=user_id)

        # ── 运行时状态 ──
        self.status: TeammateStatus = TeammateStatus.SPAWNING
        self.current_task_id: str | None = None
        self.completed_tasks: int = 0
        self.failed_tasks: int = 0
        self.last_error: str | None = None

        # ── 对话历史 (跨任务保持) ──
        self._messages: list[Any] = []

        # ── 事件驱动唤醒 ──
        self._wake_event = asyncio.Event()

        # ── 关闭 + plan approval 请求追踪 ──
        self._should_exit = False
        self._pending_requests: dict[str, dict[str, Any]] = {}  # req_id → {type, status, ...}
        self._tracker_lock = asyncio.Lock()  # s16: 并发安全锁

        # ── IDLE 超时 ──
        self._idle_rounds: int = 0
        self._max_idle_rounds: int = 12  # 12 * IDLE_POLL_INTERVAL(5s) = 60s

        # ── 控制: 延迟 auto-claim (等 Lead 规划完成后再开启) ──
        self._can_claim: bool = False

        # ── asyncio Task 引用 ──
        self._task: asyncio.Task[None] | None = None

        # ── 构建 system prompt (只构建一次) ──
        self._system_prompt = self._build_system_prompt()

        # ── 预构建中间件链 (按角色区分, 只构建一次) ──
        self._middlewares = self._build_middlewares()

        # ── 注册到消息总线 ──
        self._message_bus.register_agent(agent_name)

    # ------------------------------------------------------------------
    # System Prompt (SOUL + Team 上下文 + 协作规则 + 角色指令)
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        """构建 Teammate system prompt: SOUL + Team 上下文 + 协作规则."""
        parts: list[str] = []

        # 1. Agent SOUL
        if self._agent_soul:
            parts.append(self._agent_soul)
        elif self._agent_config and self._agent_config.description:
            parts.append(
                f"你是 {self._agent_config.display_name}, "
                f"专注于 {self._agent_config.description}。"
            )

        # 2. 项目上下文
        parts.append(self._team_context.get_project_context_xml())

        # 3. 协作规则
        parts.append(self._team_context.get_team_collaboration_rules())

        # 4. Teammate 特定指令 (按角色区分 Lead/Member, 含协议工具说明)
        parts.append(self._get_teammate_instructions())

        return "\n\n".join(parts)

    def _get_teammate_instructions(self) -> str:
        """Teammate 特定的行为指令 — 支持持续运行 +  结构化协议."""
        if self._role == "lead":
            return self._get_lead_instructions()
        return self._get_member_instructions()

    def _get_lead_instructions(self) -> str:
        """Lead Agent 专属指令."""
        return f"""<teammate_instructions>
你是 Team 的 Project Lead Agent, 名字是 **{self.name}**。

**你的核心职责:**
1. 使用 task_create 将用户目标拆解为细粒度子任务 (不指定 assigned_agent, 让 Member 自主认领)
2. 使用 spawn_teammate 动态扩充团队 (如需要特定专业领域的成员)
3. 使用 list_teammates 查看团队状态, 使用 task_list 跟踪进度
4. 使用 read_inbox 检查 Member 发来的消息 (任务完成 summary) 和审批请求
5. 收到 Member 的完成 summary 后, 评估是否需要创建新任务或调整依赖
6. 全部完成后汇总最终结果

**动态扩充团队:**
- 使用 spawn_teammate(agent_name="...") 创建新的 teammate
- 新 teammate 将自动进入 IDLE 并开始自主认领任务
- 使用 shutdown_teammate 关闭不再需要的 teammate

**澄清用户需求:**
当用户目标不清晰时, 使用 ask_clarification 工具向用户提问:
- 目标描述过于模糊, 无法拆解为具体任务
- 存在多种合理的实现方案, 需要用户选择
- 缺少关键信息 (如技术栈、目标平台、性能要求等)
ask_clarification 会暂停当前执行, 等待用户回答后再继续。

** 协议工具:**
- 使用 shutdown_teammate 向指定 Member 发起 shutdown_request (关机握手)
- 收到 plan_approval_request 时, 审阅计划后使用 approve_plan 工具回复:
  - 批准: approve_plan(request_id="...", requester="...", approve=True, feedback="...")
  - 拒绝: approve_plan(request_id="...", requester="...", approve=False, feedback="拒绝原因")
- 收到 shutdown_response 时, 记录 teammate 的关机确认

**通信:**
- 使用 broadcast 向全员发送通知
- 使用 send_message 向特定 Member 发送私聊
- 使用 read_inbox 检查收件箱
</teammate_instructions>"""

    def _get_member_instructions(self) -> str:
        """Member Agent 专属指令 —  关机由 LLM 决策."""
        return f"""<teammate_instructions>
你是 Team 中的一名成员, 名字是 **{self.name}**。
你是一个 **持久化运行的 Agent**, 不是一次性工具调用。

**你的生命周期:**
- WORKING: 执行分配的任务或自主认领的任务, 使用你的工具和专业知识
- IDLE: 任务完成后回到 IDLE, 等待新任务或消息
- IDLE 超时 (60s 无任务) 后自动退出

**任务执行规则:**
1. 收到任务后使用 task_update 将状态改为 in_progress
2. 按步骤完成任务
3. 完成后使用 task_update 将状态改为 completed 并附上结果
4. 失败时使用 task_update 将状态改为 failed 并说明原因

**通信规则:**
1. 使用 send_message 向其他 Agent 发送消息
2. 遇到阻塞或需要澄清时, 主动发消息给 Lead
3. 使用 read_inbox 检查是否有新消息

** 结构化协议工具:**
- 收到 shutdown_request 时, 评估当前工作状态后使用 shutdown_response 工具回复:
  - 批准: shutdown_response(request_id="...", requester="...", approve=True)
  - 拒绝: shutdown_response(request_id="...", requester="...", approve=False, reason="正在执行关键任务...")
  - 批准后 Agent 将完成当前工具调用后优雅退出 (不丢失数据)
- 高风险操作前, 使用 request_plan_approval 向 Lead 提交计划, 等待 approve_plan 审批结果

** 自主行为:**
- IDLE 时自动扫描任务板, 使用 claim_task 认领未分配的任务
- 使用 idle 声明自己空闲, 等待新任务

**子任务委派:**
- 使用 task 工具将复杂任务的子步骤委派给 SubAgent 并行执行
- SubAgent 是一次性的: 接收 instruction → 执行 → 返回结果
</teammate_instructions>"""

    # ------------------------------------------------------------------
    # 中间件构建 (按角色区分)
    # ------------------------------------------------------------------

    def _build_middlewares(self) -> list[AgentMiddleware]:
        """构建中间件链 — Lead 保留 DynamicContext/Todo/Subagent, Member 排除."""
        from typing import Callable
        from langchain.agents.middleware import AgentMiddleware
        from harness.team.teammate_middleware import build_teammate_middlewares
        from harness.config.paths import get_paths

        paths = get_paths()
        workspace_root = str(paths.sandbox_work_dir(self.name))

        is_lead = self._role == "lead"

        # ── InboxDrainMiddleware: 每次 LLM 调用前 drain inbox + 检查 shutdown ──
        _self = self

        class InboxDrainMiddleware(AgentMiddleware):
            async def awrap_model_call(self, request: Any, handler: Callable) -> Any:
                inbox = await _self._message_bus.read_inbox(_self.name)
                for msg in inbox:
                    await _self._handle_inbox_message(msg)
                if _self._should_exit:
                    raise asyncio.CancelledError("Teammate shutdown requested")
                return await handler(request)

        middlewares = build_teammate_middlewares(
            workspace_root=workspace_root,
            agent_name=self.name,
            is_plan_mode=is_lead,           # Lead 需要 TodoMiddleware
            subagent_enabled=not is_lead,   # 仅 Member 可委派子任务, Lead 通过 delegate_to_member
            memory_enabled=True,
            summarization_enabled=True,
            guardrail_enabled=True,
            vision_enabled=getattr(self._agent_config, 'vision', False) if self._agent_config else False,
            tool_max_retries=3,
            # Lead 额外保留 DynamicContextMiddleware + ClarificationMiddleware
            keep_dynamic_context=is_lead,
            keep_clarification=is_lead,
            custom_middlewares=[InboxDrainMiddleware()],
        )
        return middlewares

    # ------------------------------------------------------------------
    # 生命周期管理
    # ------------------------------------------------------------------

    async def spawn(self, initial_task: TeamTask | None = None) -> None:
        """启动 teammate 的 agent loop."""
        self.status = TeammateStatus.SPAWNING

        if initial_task:
            self.current_task_id = initial_task.id
            self._messages.append(HumanMessage(
                content=f"[新任务 {initial_task.id}] {initial_task.title}\n\n{initial_task.description}"
            ))

        self.status = TeammateStatus.IDLE
        self._task = asyncio.create_task(self._agent_loop())
        logger.info("Teammate '%s' spawned (idle, waiting for tasks)", self.name)

    async def shutdown(self) -> None:
        """请求关闭 — 发送 shutdown_request 到自己的 inbox, 触发优雅退出."""
        self._should_exit = True
        self._wake_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._message_bus.unregister_agent(self.name)
        self.status = TeammateStatus.SHUTDOWN
        logger.info("Teammate '%s' shut down", self.name)

    def enable_auto_claim(self) -> None:
        """开启自主认领 — orchestrator 在 Lead 规划完成后调用."""
        self._can_claim = True
        self._wake_event.set()  # 唤醒 IDLE loop 立即开始扫描
        logger.info("Teammate '%s' auto-claim enabled", self.name)

    # ------------------------------------------------------------------
    # 核心循环
    # ------------------------------------------------------------------

    async def _agent_loop(self) -> None:
        """主循环 — 持续运行直到 shutdown."""
        # 设置当前 Agent 上下文 (工具通过 ContextVar 获取身份和实例引用)
        from harness.team.tools import set_current_agent, set_current_agent_instance
        set_current_agent(self.name)
        set_current_agent_instance(self)

        while not self._should_exit:
            if self.status == TeammateStatus.IDLE:
                await self._idle_loop()
            elif self.status == TeammateStatus.WORKING:
                await self._work_loop()
            elif self.status == TeammateStatus.SHUTTING_DOWN:
                break

        self.status = TeammateStatus.SHUTDOWN

    # ------------------------------------------------------------------
    # IDLE 阶段 — 事件驱动等待
    # ------------------------------------------------------------------

    async def _idle_loop(self) -> None:
        """IDLE 阶段 — 事件驱动等待消息或新任务.

        参考  每 IDLE_POLL_INTERVAL 秒检查一次 inbox.
        参考 : IDLE 时自主扫描任务板认领, 超时自动 SHUTDOWN.
        """
        self._idle_rounds = 0
        while self.status == TeammateStatus.IDLE and not self._should_exit:
            self._idle_rounds += 1

            # ── : IDLE 超时 → 自动 SHUTDOWN ──
            if self._idle_rounds > self._max_idle_rounds:
                logger.info("Teammate '%s' idle timeout (%ds), auto-shutting down",
                            self.name, self._idle_rounds * int(IDLE_POLL_INTERVAL))
                self._should_exit = True
                break

            try:
                # 事件驱动等待 (有消息时立即唤醒, 或超时后检查)
                await asyncio.wait_for(self._wake_event.wait(), timeout=IDLE_POLL_INTERVAL)
                self._wake_event.clear()
                self._idle_rounds = 0  # 被唤醒, 重置超时计数
            except asyncio.TimeoutError:
                pass  # 超时, 正常轮询

            # 1. 检查 inbox
            inbox = await self._message_bus.read_inbox(self.name)
            for msg in inbox:
                await self._handle_inbox_message(msg)

            # 2. : 自主扫描任务板认领
            if self.status == TeammateStatus.IDLE:
                claimed = await self._maybe_claim_task()
                if claimed:
                    self._idle_rounds = 0  # 认领成功, 重置超时

    # ------------------------------------------------------------------
    # WORKING 阶段 — 完整 ReAct agent loop
    # ------------------------------------------------------------------

    async def _work_loop(self) -> None:
        """WORKING 阶段 — create_agent() + 预构建中间件 + astream_events.

        中间件链在 __init__ 中按角色区分:
          Lead:   DynamicContext + Todo + Clarification, 无 SubagentLimit (委派走 delegate_to_member)
          Member: SubagentLimit (可用 task 委派子任务), 无 DynamicContext/Todo/Clarification
          Lead 层数 ≈19, Member 层数 ≈18
        统一排除: TitleMiddleware
        """
        from langchain.agents import create_agent
        from langchain_core.runnables import RunnableConfig

        # ── s17: 身份重注入 (防止长上下文后遗忘) ──
        self._inject_identity()

        # ── 只保留最近消息防止上下文爆炸 ──
        recent = self._messages[-50:] if len(self._messages) > 50 else self._messages

        # ── Tracing: 标记工作开始 ──
        if self._tracer is not None:
            self._tracer.trace_teammate_work_start(
                self.name, self.current_task_id, role=self._role,
            )

        # ── create_agent + astream_events ──
        try:
            agent = create_agent(
                model=self.llm,
                tools=self._tools,
                system_prompt=self._system_prompt,
                middleware=self._middlewares,
            )

            max_turns = self._agent_config.max_turns if self._agent_config else MAX_WORK_TURNS
            # ── Tracing: LangChain callback 自动追踪 LLM + Tool 调用 ──
            callbacks = []
            if self._tracer is not None and self._tracer.is_enabled:
                lc_callback = self._tracer.get_langchain_callback()
                if lc_callback is not None:
                    callbacks.append(lc_callback)
            config = RunnableConfig(
                configurable={"thread_id": f"teammate-{self.name}-{uuid.uuid4()}"},
                recursion_limit=max_turns * 3,
                callbacks=callbacks if callbacks else None,
            )

            input_state = {"messages": recent} if recent else {"messages": []}
            final_messages: list[Any] = []

            # astream_events — 推送实时思考 / 工具调用事件到 SSE 流
            async for event in agent.astream_events(input_state, config, version="v2"):
                kind = event.get("event", "")
                data: dict[str, Any] = event.get("data", {})  # type: ignore[assignment]

                if kind == "on_chat_model_stream":
                    chunk = data.get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        self._push_event({
                            "type": "message",
                            "content": str(chunk.content),
                            "subagent_name": self.name,
                        })

                elif kind == "on_tool_start":
                    tool_name = event.get("name", "")
                    tool_input = data.get("input", {})
                    # 跳过内部工具 (task_create 等 team 工具)
                    self._push_event({
                        "type": "tool_call",
                        "subagent_name": self.name,
                        "tool_name": tool_name,
                        "tool_args": tool_input if isinstance(tool_input, dict) else {},
                    })

                elif kind == "on_tool_end":
                    tool_name = event.get("name", "")
                    tool_output = data.get("output", "")
                    output_str = str(tool_output)[:500]
                    self._push_event({
                        "type": "tool_result",
                        "subagent_name": self.name,
                        "tool_name": tool_name,
                        "tool_result": output_str,
                    })

                elif kind == "on_chain_end" and event.get("name") == "LangGraph":
                    result = data.get("output", {})
                    if isinstance(result, dict):
                        final_messages = list(result.get("messages", []))

            # 回写消息历史 — 保留执行期间收到的 inbox 消息
            if final_messages:
                self._messages = list(final_messages)
            # drain 执行期间可能到达的残余 inbox 消息并追加
            late_inbox = await self._message_bus.read_inbox(self.name)
            for msg in late_inbox:
                await self._handle_inbox_message(msg)
            logger.info("Teammate '%s' completed task (output: %d messages, late inbox: %d)",
                        self.name, len(self._messages), len(late_inbox))

        except asyncio.CancelledError:
            logger.info("Teammate '%s' work cancelled (shutdown)", self.name)
        except Exception as exc:
            logger.error("Teammate '%s' work_loop failed: %s", self.name, exc)
            self.last_error = str(exc)
            if self.current_task_id:
                await self._task_store.update_task(
                    self.current_task_id,
                    status=TeamTaskStatus.FAILED,
                    error=str(exc),
                )
                # ── Tracing: 任务失败事件 ──
                if self._tracer is not None:
                    self._tracer.trace_task_event(
                        self.current_task_id, "failed",
                        metadata={"agent_name": self.name, "error": str(exc)},
                    )

        # ── 任务完成 → 回到 IDLE (或被 shutdown 打断 → SHUTTING_DOWN) ──
        completed_task_id = self.current_task_id
        if self.current_task_id:
            self.completed_tasks += 1
            self.current_task_id = None
            # ── Tracing: 任务完成事件 ──
            if self._tracer is not None:
                self._tracer.trace_task_event(
                    completed_task_id, "completed",
                    metadata={"agent_name": self.name, "role": self._role},
                )

        # ── Tracing: 工作结束 ──
        if self._tracer is not None:
            self._tracer.trace_teammate_work_end(
                self.name, completed_task_id, role=self._role,
                status="failed" if self.last_error else "completed",
            )

        # ── Member 完成后发 summary 给 Lead ──
        if completed_task_id and self._role != "lead" and self._lead_name:
            try:
                task = await self._task_store.get_task(completed_task_id)
                if task:
                    summary = (
                        f"完成任务 [{task.id}] {task.title}\n"
                        f"状态: {task.status.value}\n"
                        f"输出: {task.output[:500] if task.output else '(无输出)'}"
                    )
                    await self._message_bus.send(TeamMessage(
                        from_agent=self.name, to_agent=self._lead_name,
                        msg_type=TeamMessageType.TEXT,
                        content=summary, task_id=task.id,
                    ))
            except Exception as exc:
                logger.warning("Teammate '%s' failed to send summary to Lead: %s", self.name, exc)

        if self._should_exit:
            self.status = TeammateStatus.SHUTTING_DOWN
            logger.info("Teammate '%s' work_loop: shutdown flag set, entering SHUTTING_DOWN", self.name)
        else:
            self.status = TeammateStatus.IDLE

    # ------------------------------------------------------------------
    # 外部唤醒
    # ------------------------------------------------------------------

    async def assign_task(self, task: TeamTask) -> None:
        """外部唤醒: 分配任务给此 teammate."""
        if self.status != TeammateStatus.IDLE:
            logger.warning(
                "Teammate '%s' is not idle (status=%s), cannot assign task",
                self.name, self.status,
            )
            return

        self.current_task_id = task.id
        self._messages.append(HumanMessage(
            content=(
                f"<assigned_task>\n"
                f"  <task_id>{task.id}</task_id>\n"
                f"  <title>{task.title}</title>\n"
                f"  <description>{task.description}</description>\n"
                f"  <priority>{task.priority}</priority>\n"
                f"</assigned_task>\n\n"
                f"请完成上述任务。完成后使用 task_update 将状态改为 completed 并附上结果。\n"
                f"如果失败, 使用 task_update 将状态改为 failed 并说明原因。"
            )
        ))
        self.status = TeammateStatus.WORKING
        self._wake_event.set()
        logger.info("Teammate '%s' assigned task '%s'", self.name, task.id)

    # ------------------------------------------------------------------
    # 消息处理 
    # ------------------------------------------------------------------

    async def _handle_inbox_message(self, msg: TeamMessage) -> None:
        """处理收件箱消息 —  结构化协议在此路由."""
        # ──  shutdown_request → 注入消息让 LLM 决策
        #  关键设计: 不再硬编码批准, 而是让 LLM 评估当前工作状态后调用
        #  shutdown_response 工具 (approve=True/False) 来决定是否关机.
        if msg.msg_type == TeamMessageType.SHUTDOWN_REQUEST:
            req_id = msg.request_id or str(uuid.uuid4())[:8]
            async with self._tracker_lock:
                self._pending_requests[req_id] = {
                    "type": "shutdown",
                    "status": RequestStatus.PENDING,
                    "from": msg.from_agent,
                }
            # 注入到消息历史, LLM 在下一轮推理中看到并决策
            self._messages.append(HumanMessage(
                content=(
                    f"<shutdown_request>\n"
                    f"  <request_id>{req_id}</request_id>\n"
                    f"  <from>{msg.from_agent}</from>\n"
                    f"  <message>{msg.content}</message>\n"
                    f"</shutdown_request>\n\n"
                    f"你收到了来自 '{msg.from_agent}' 的关机请求。请评估当前工作状态后使用 shutdown_response 工具回复:\n"
                    f"- 如果当前没有正在执行的关键任务, 批准关机:\n"
                    f"  shutdown_response(request_id=\"{req_id}\", requester=\"{msg.from_agent}\", approve=True)\n"
                    f"- 如果正在执行关键任务 (如写文件), 拒绝关机:\n"
                    f"  shutdown_response(request_id=\"{req_id}\", requester=\"{msg.from_agent}\", approve=False, reason=\"正在执行关键任务...\")\n\n"
                    f"注意: approve=True 后 Agent 将在当前轮次结束后优雅退出 (完成当前工具调用后再退出)。"
                )
            ))
            if self.status == TeammateStatus.IDLE:
                self.status = TeammateStatus.WORKING
            logger.info("Teammate '%s' received shutdown_request (%s), injected into messages for LLM decision",
                        self.name, req_id)
            return

        # ── shutdown_response — Lead 收到 teammate 的确认 ──
        if msg.msg_type == TeamMessageType.SHUTDOWN_RESPONSE:
            async with self._tracker_lock:
                if msg.request_id and msg.request_id in self._pending_requests:
                    new_status = RequestStatus.APPROVED if "approved" in msg.content else RequestStatus.REJECTED
                    self._pending_requests[msg.request_id]["status"] = new_status
            logger.info("Teammate '%s' received shutdown_response from '%s': %s",
                        self.name, msg.from_agent, msg.content)
            return

        # ── plan_approval_request — Lead 收到 teammate 的审批请求 ──
        if msg.msg_type == TeamMessageType.PLAN_APPROVAL_REQUEST:
            req_id = msg.request_id or str(uuid.uuid4())[:8]
            async with self._tracker_lock:
                self._pending_requests[req_id] = {
                    "type": "plan_approval",
                    "status": RequestStatus.PENDING,
                    "from": msg.from_agent,
                    "plan": msg.content,
                }
            self._messages.append(HumanMessage(
                content=(
                    f"<plan_approval_request>\n"
                    f"  <request_id>{req_id}</request_id>\n"
                    f"  <from>{msg.from_agent}</from>\n"
                    f"  <plan>{msg.content}</plan>\n"
                    f"</plan_approval_request>\n\n"
                    f"来自 '{msg.from_agent}' 的计划审批请求。请审阅后使用 approve_plan 工具回复:\n"
                    f"- 批准: approve_plan(request_id=\"{req_id}\", requester=\"{msg.from_agent}\", approve=True, feedback=\"补充建议...\")\n"
                    f"- 拒绝: approve_plan(request_id=\"{req_id}\", requester=\"{msg.from_agent}\", approve=False, feedback=\"拒绝原因...\")\n\n"
                    f"审批标准: 计划是否安全? 是否与项目目标一致? 是否有更优方案?"
                )
            ))
            if self.status == TeammateStatus.IDLE:
                self.status = TeammateStatus.WORKING
            return

        # ── plan_approval_response — Teammate 收到 Lead 的审批结果 ──
        if msg.msg_type == TeamMessageType.PLAN_APPROVAL_RESPONSE:
            async with self._tracker_lock:
                if msg.request_id and msg.request_id in self._pending_requests:
                    new_status = RequestStatus.APPROVED if "approved" in msg.content else RequestStatus.REJECTED
                    self._pending_requests[msg.request_id]["status"] = new_status
            self._messages.append(HumanMessage(
                content=(
                    f"<plan_approval_response>\n"
                    f"  <request_id>{msg.request_id}</request_id>\n"
                    f"  <from>{msg.from_agent}</from>\n"
                    f"  <result>{msg.content}</result>\n"
                    f"</plan_approval_response>"
                )
            ))
            if self.status == TeammateStatus.IDLE:
                self.status = TeammateStatus.WORKING
            return

        # lifecycle 通知 — 唤醒 Lead 让其监控团队进度
        if msg.msg_type == TeamMessageType.LIFECYCLE:
            self._messages.append(HumanMessage(
                content=(
                    f"[团队通知] {msg.from_agent} {msg.content}\n\n"
                    f"作为 Lead, 请检查团队状态: 使用 list_teammates 查看成员状态, "
                    f"使用 task_list 查看任务进度。如果有需要, 可以创建新任务或重新分配。"
                )
            ))
            if self.status == TeammateStatus.IDLE:
                self.status = TeammateStatus.WORKING
            return

        # 普通消息 / 广播
        self._messages.append(HumanMessage(
            content=f"[来自 {msg.from_agent}] {msg.content}"
        ))
        if self.status == TeammateStatus.IDLE:
            self.status = TeammateStatus.WORKING

    # ------------------------------------------------------------------
    #  预留: 自主认领
    # ------------------------------------------------------------------

    async def _maybe_claim_task(self) -> bool:
        """: IDLE 时自主扫描任务板并认领未分配的任务.

        认领条件:
        1. _can_claim == True (orchestrator 在 Lead 规划完成后开启)
        2. status == PENDING
        3. assigned_agent is None (无 owner)
        4. 所有依赖已完成

        Returns:
            True 如果成功认领了一个任务.
        """
        #  门控: 等待 orchestrator 开启 auto-claim
        if not self._can_claim:
            return False

        try:
            unclaimed = await self._task_store.get_unclaimed_tasks()
        except AttributeError:
            return False  # task_store 版本不支持此方法

        if not unclaimed:
            return False

        # 角色过滤: Lead 不认领执行任务
        role = "lead" if (self._agent_config and self._agent_config.can_be_lead) else "member"

        for task in unclaimed:
            # 跳过自己创建的任务 (Lead 创建的规划任务)
            # 简单启发: 标题包含 "规划:" 的不认领
            if task.title.startswith("规划:"):
                continue
            # 认领第一个符合条件的任务
            await self._task_store.update_task(
                task.id,
                assigned_agent=self.name,
                status=TeamTaskStatus.IN_PROGRESS,
            )
            self.current_task_id = task.id
            self._messages.append(HumanMessage(
                content=(
                    f"[自主认领任务 {task.id}] {task.title}\n\n{task.description}\n\n"
                    f"请完成上述任务。完成后使用 task_update 将状态改为 completed 并附上结果。"
                )
            ))
            self.status = TeammateStatus.WORKING
            logger.info("Teammate '%s' (role=%s) auto-claimed task '%s': %s",
                        self.name, role, task.id, task.title[:60])
            return True

        return False

    def _inject_identity(self) -> None:
        """: 注入身份块 — 防止长上下文后遗忘自己是谁.

        在每次 _work_loop 开始前调用, 确保 SOUL 和角色在上下文中可见.
        """
        identity = (
            f"<identity>\n"
            f"  <name>{self.name}</name>\n"
            f"  <status>{self.status.value}</status>\n"
            f"  <completed_tasks>{self.completed_tasks}</completed_tasks>\n"
            f"</identity>"
        )
        # 只在消息历史末尾追加轻量身份标记
        if self._messages and hasattr(self._messages[-1], 'content'):
            last_content = str(self._messages[-1].content)
            if "<identity>" not in last_content:
                self._messages.append(HumanMessage(content=identity))

    # ------------------------------------------------------------------
    # SSE 事件推送
    # ------------------------------------------------------------------

    def _push_event(self, event: dict[str, Any]) -> None:
        """推送事件到 orchestrator 的 SSE 流 (非阻塞)."""
        if self._event_queue is not None:
            try:
                self._event_queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # 队列满了就丢弃, 不阻塞 agent loop

    # ------------------------------------------------------------------
    # 状态导出
    # ------------------------------------------------------------------

    def to_runtime(self) -> TeamMemberRuntime:
        """导出为 TeamMemberRuntime (兼容现有接口)."""
        return TeamMemberRuntime(
            agent_name=self.name,
            role="lead" if (self._agent_config and self._agent_config.can_be_lead) else "member",
            status=self.status,
            current_task_id=self.current_task_id,
            completed_tasks=self.completed_tasks,
            failed_tasks=self.failed_tasks,
            last_error=self.last_error,
        )
