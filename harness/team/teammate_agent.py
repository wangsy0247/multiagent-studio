"""TeammateAgent — 持久化 Teammate, 拥有自己的 agent loop.

_teammate_loop 设计:
- 独立 asyncio Task 持续运行
- WORKING 阶段: 完整 ReAct agent loop, 多轮 LLM 推理
- IDLE 阶段: 事件驱动等待消息, 不扫描任务板
- 完成后回到 IDLE, 不销毁 — 跨任务保持上下文
- shutdown_request/plan_approval 协议消息处理
- 任务分配统一由 Orchestrator._dispatch_ready_tasks() 负责

设计: _agent_loop() 持续运行 → IDLE → 被唤醒 → WORKING → IDLE → ...
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool

from harness.config.agents_config import load_agent_config, load_agent_soul
from harness.models import HarnessState
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
from harness.memory.task_memory import TaskMemoryStore
from harness.memory.member_memory import MemberMemoryStore, extract_lessons_from_task
from harness.team.agent_logs import AgentLogWriter
from harness.team.task_store import TeamTaskStore

logger = logging.getLogger(__name__)

# ── 常量 ──
IDLE_POLL_INTERVAL = 5.0       # IDLE 时 inbox 检查间隔 (秒)
MAX_WORK_TURNS = 50            # WORKING 阶段最大 LLM 轮次

# ── Lead 防漂移: 每 N 轮 LLM 调用复述一次调度要点 (长会话中 prompt 会被稀释) ──
LEAD_REMINDER_INTERVAL = 5
LEAD_COORDINATOR_REMINDER = (
    "<system-reminder>\n"
    "Coordinator key-points recap:\n"
    "1. Four-phase progression: research → synthesis → delegated implementation → verification; you are a pure coordinator — always delegate implementation work.\n"
    "2. Delegation must be self-contained: members cannot see your conversation history; each task must include background/goal/constraints and be independently executable.\n"
    "3. Create independent subtasks simultaneously for parallel execution — parallelism is your superpower.\n"
    "4. Separate implementation from verification: the executor must not be the sole verifier of their own output.\n"
    "</system-reminder>"
)


def _resolve_approval_status(msg: TeamMessage) -> RequestStatus:
    """解析协议响应 (shutdown_response / plan_approval_response) 的审批结果.

    优先读结构化 approved 字段 (发送方显式写入);
    字段缺失 (旧消息) 时回退到 content 首行前缀判定 — 不做全文本子串匹配,
    避免拒绝文案 (如 "not approved yet") 含 "approved" 被误判为批准.
    """
    if msg.approved is not None:
        return RequestStatus.APPROVED if msg.approved else RequestStatus.REJECTED
    # ── 回退: 兼容无 approved 字段的旧消息 — 仅看首行前缀 ──
    first_line = (msg.content or "").strip().lower()
    first_line = first_line.splitlines()[0] if first_line else ""
    if first_line.startswith("approved"):
        return RequestStatus.APPROVED
    return RequestStatus.REJECTED


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
        task_memory_store: TaskMemoryStore | None = None,
        member_memory_store: MemberMemoryStore | None = None,
        skill_storage: Any | None = None,
        event_queue: asyncio.Queue | None = None,
        role: str = "member",
        lead_name: str | None = None,
        thread_id: str = "",
        project_id: str = "",
        tracer: Any = None,
        effective_config: Any = None,
        soul_override: str | None = None,
        checkpointer: Any = None,  # LangGraph BaseCheckpointSaver | None
        llm_semaphore: asyncio.Semaphore | None = None,  # LLM 并发控制
        skill_evolution_store: Any = None,  # Phase 5: 成员技能进化存储 (可注入, 便于测试)
        worktree_virtual_path: str = "",    # Phase 6: worktree 隔离成员的 sandbox 工作区路径 (空=shared)
    ) -> None:
        self.name = agent_name
        self.llm = llm
        self._tools = tools
        self._team_context = team_context
        self._message_bus = message_bus
        self._task_store = task_store
        self._task_memory_store = task_memory_store
        self._member_memory_store = member_memory_store
        # ── 当前任务文本缓存 (assign_task 时写入, 结算时清空) —
        # 供 _build_system_prompt 按任务相关性检索 L3 成员经验 ──
        self._current_task_text: str = ""
        self._skill_storage = skill_storage
        self._event_queue = event_queue
        self._role = role
        self._lead_name = lead_name
        self._thread_id = thread_id
        self._project_id = project_id
        self._tracer = tracer
        self._checkpointer = checkpointer
        self._llm_semaphore = llm_semaphore
        # ── Phase 6: worktree 隔离 (空字符串 = 默认 shared, prompt 不注入隔离段) ──
        self._worktree_virtual_path = worktree_virtual_path

        # ── Phase 5: 成员私有技能进化 (仅 member; Lead 不参与) ──
        self._skill_evolution_store = skill_evolution_store
        if self._skill_evolution_store is None and role != "lead":
            try:
                from harness.skills.evolution.member import MemberSkillEvolutionStore
                self._skill_evolution_store = MemberSkillEvolutionStore(
                    user_id=team_context.user_id,
                )
            except Exception:
                logger.debug(
                    "Failed to init skill evolution store for '%s'", agent_name,
                    exc_info=True,
                )
        self._skill_candidate_extracted = False  # 每 run 每成员限提取 1 个候选
        # skill 加载统计 (spawn 自检用): 区分"无技能"与"加载失败"
        self._skills_stats: dict[str, Any] = {
            "loaded": 0, "after_whitelist": 0, "load_failed": False,
        }

        # ── EffectiveConfig (优先) + 向后兼容旧 AgentConfig ──
        self._effective_config = effective_config
        self._user_id = team_context.user_id

        # ── SOUL 解析: soul_override > effective_config.agent_soul > SOUL.md ──
        if soul_override is not None:
            self._agent_config = None
            self._agent_soul = soul_override
        elif effective_config is not None:
            self._agent_config = None  # 统一用 effective_config
            self._agent_soul = effective_config.agent_soul
        else:
            # Fallback: 兼容未传入 effective_config 的旧调用路径
            user_id = team_context.user_id
            self._agent_config = load_agent_config(agent_name, user_id=user_id)
            self._agent_soul = load_agent_soul(agent_name, user_id=user_id)

        # ── 运行时状态 ──
        self.status: TeammateStatus = TeammateStatus.SPAWNING
        self.current_task_id: str | None = None
        self._current_task_is_synthesis = False  # usage ledger 归因用 (lead 汇总任务)
        self.completed_tasks: int = 0
        self.failed_tasks: int = 0
        self.last_error: str | None = None

        # ── 稳定 checkpoint thread_id (跨 graph run 持久化状态) ──
        # 格式: team-{project_id}-{thread_id}-{agent_name}
        # checkpointer=None 时退化为随机 ID (无持久化)
        _pid = project_id or "noproject"
        _tid = thread_id or "nothread"
        if checkpointer is not None:
            self._checkpoint_thread_id = f"team-{_pid}-{_tid}-{agent_name}"
        else:
            self._checkpoint_thread_id = f"teammate-{agent_name}-{uuid.uuid4()}"

        # ── 对话历史 (跨任务保持, checkpointer 启用时自动从 state 恢复) ──
        self._messages: list[Any] = []

        # ── 任务间上下文裁剪: 积累已完成任务的摘要, 下一任务注入 ──
        self._task_summaries: list[str] = []

        # ── 事件驱动唤醒 ──
        # 与消息总线的 per-agent 通知事件是同一个对象: send() → _notify() 即唤醒,
        # 复用 message_bus 的 Event, 消息到达时实时唤醒
        self._wake_event = message_bus.get_event(agent_name)

        # ── 关闭 + plan approval 请求追踪 ──
        self._should_exit = False
        self._pending_requests: dict[str, dict[str, Any]] = {}  # req_id → {type, status, ...}
        self._tracker_lock = asyncio.Lock()  # s16: 并发安全锁

        # ── asyncio Task 引用 ──
        self._task: asyncio.Task[None] | None = None

        # ── Title 去重标志 (仅 Lead, 防止多次 graph run 重复生成标题) ──
        self._title_emitted = [False] if role == "lead" else None

        # ── 构建 system prompt (初始; 每个工作周期在 _work_loop 开头重建以反映最新团队状态) ──
        self._system_prompt = self._build_system_prompt()

        # ── Agent 对话日志写入器 (供前端按 agent 隔离展示) ──
        self._agent_log_writer: AgentLogWriter | None = None

        # ── AI 流式消息 buffer (member agent 用, 积累 chunks 后写入 JSONL) ──
        self._streaming_ai_buffer: str = ""
        self._streaming_ai_task_id: str | None = None

        # ── 暂停/恢复: pending clarification (Lead 调用 ask_clarification 后) ──
        self.pending_clarification: dict[str, Any] | None = None

        # ── 预构建中间件链 (按角色区分, 只构建一次) ──
        self._middlewares = self._build_middlewares()

        # ── 注册到消息总线 ──
        self._message_bus.register_agent(agent_name)

    # ------------------------------------------------------------------
    # System Prompt (SOUL + Team 上下文 + 协作规则 + 角色指令)
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        """构建 Teammate system prompt: SOUL + Team 上下文 + 协作规则 + Skills."""
        parts: list[str] = []

        # 1. Agent SOUL
        if self._agent_soul:
            parts.append(self._agent_soul)
        elif self._agent_config and self._agent_config.description:
            parts.append(
                f"You are {self._agent_config.display_name}, "
                f"specialized in {self._agent_config.description}."
            )

        # 2. 项目上下文 (含成员能力卡片)
        parts.append(self._team_context.get_project_context_xml())

        # 3. 团队记忆 (L2 — 跨运行积累的协作知识)
        team_memory_xml = self._team_context.get_team_memory_xml()
        if team_memory_xml:
            parts.append(team_memory_xml)

        # 3.5 成员经验记忆 (仅 Member; Lead 用 L0+L2, 不注入 L1/L3)
        # L1 成员全局经验全量注入 (每条截断); L3 项目×成员经验按当前任务
        # 相关性检索 top-K 注入 (无任务文本时跳过检索只注入 L1)
        if self._role != "lead" and self._member_memory_store is not None:
            try:
                l1_xml = self._member_memory_store.get_l1_context(self.name)
                if l1_xml:
                    parts.append(l1_xml)
                if self._current_task_text and self._project_id:
                    l3_xml = self._member_memory_store.get_l3_context(
                        self._project_id, self.name, self._current_task_text,
                    )
                    if l3_xml:
                        parts.append(l3_xml)
            except Exception:
                logger.debug(
                    "Failed to inject member memory for '%s'", self.name, exc_info=True,
                )

        # 4. 团队能力矩阵 (agent cards — 所有成员可见)
        capabilities_xml = self._team_context.get_team_capabilities_xml()
        if capabilities_xml:
            parts.append(capabilities_xml)

        # 5. 协作规则
        parts.append(self._team_context.get_team_collaboration_rules())

        # 6. Teammate 特定指令 (按角色区分 Lead/Member, 含协议工具说明)
        parts.append(self._get_teammate_instructions())

        # 7. Skills (仅 Member, Lead 不需要 skill 能力)
        if self._role != "lead":
            skills_section = self._build_skills_section()
            if skills_section:
                parts.append(skills_section)

        # 8. 延迟加载工具清单 (仅当本 agent 持有 deferred MCP 工具时)
        from harness.tools.tool_search import (
            get_deferred_prompt_section,
            get_deferred_setup,
        )
        _ts_setup = get_deferred_setup()
        if _ts_setup is not None and any(
            t.name in _ts_setup.deferred_names for t in self._tools
        ):
            deferred_section = get_deferred_prompt_section()
            if deferred_section:
                parts.append(deferred_section)

        return "\n\n".join(parts)

    def _get_skill_whitelist(self) -> set[str] | None:
        """Return the agent-level skill whitelist from effective config.

        * ``None`` — no whitelist configured → all enabled skills available.
        * ``set()`` (empty) — explicitly empty → no skills at all.
        * ``{"a", "b"}`` — only the named skills.
        """
        # EffectiveConfig.skills (default factory=list → [] = "not configured")
        if self._effective_config is not None:
            raw = self._effective_config.skills
            if raw:  # non-empty list → explicit whitelist
                return set(raw)
            # empty list + has agent_config skills? → fall through to check
        # Fallback: old-style agent_config
        if self._agent_config is not None:
            skills_attr = getattr(self._agent_config, "skills", None)
            if skills_attr is not None:
                return set(skills_attr)
        return None

    def _build_skills_section(self) -> str:
        """Build the ``<skill_system>`` prompt block for this teammate.

        Loads enabled skills scoped to the current user, then applies the
        agent-level whitelist.  Returns an empty string when no skills are
        available or skill_storage is not configured.

        Phase 5: 记录加载统计供 spawn 自检 (区分"无技能"与"加载失败");
        末尾追加成员私有进化技能块 (probation 标注"试验性")。
        """
        section = ""
        if self._skill_storage is not None:
            skills: list[Any] = []
            load_failed = False
            try:
                skills = self._skill_storage.load_skills(
                    enabled_only=True, user_id=self._user_id,
                )
            except Exception:
                # ── 失败要可见: warning 级别 + 统计标记, 不再静默吞掉 ──
                load_failed = True
                logger.warning(
                    "Failed to load skills for teammate '%s' — "
                    "本周期将不带平台技能运行", self.name, exc_info=True,
                )
            loaded_count = len(skills)

            # Apply agent-level whitelist
            whitelist = self._get_skill_whitelist()
            if whitelist is not None:
                skills = [s for s in skills if s.name in whitelist]

            # per-agent skill 开关 (extensions_config.yaml 黑名单)
            if self._effective_config is not None:
                from harness.skills.filter import filter_skills_by_agent
                skills = filter_skills_by_agent(
                    skills, self._effective_config.enabled_skills,
                )

            self._skills_stats = {
                "loaded": loaded_count,
                "after_whitelist": len(skills),
                "load_failed": load_failed,
                "available_names": {s.name for s in skills},
            }

            if skills:
                from harness.skills.prompt import get_skills_prompt_section

                try:
                    from harness.skills.cache import (
                        build_skills_signature,
                        get_cached_skills_prompt_section,
                    )
                    sig = build_skills_signature(skills)
                    section = get_cached_skills_prompt_section(
                        sig,
                        lambda: get_skills_prompt_section(skills),
                    )
                except Exception:
                    section = get_skills_prompt_section(skills)

        # ── Phase 5: 成员私有进化技能 (probation + active; archived 不注入) ──
        evolved = self._evolved_skills_section()
        return "\n\n".join(part for part in (section, evolved) if part)

    def _evolved_skills_section(self) -> str:
        """渲染成员私有进化技能块 (Phase 5); 无存储或无技能时返回空串."""
        store = self._skill_evolution_store
        if store is None:
            return ""
        try:
            from harness.skills.evolution.member import render_evolved_skills_section
            records = store.list_skills(self.name)
            return render_evolved_skills_section(records)
        except Exception:
            logger.debug(
                "Failed to load evolved skills for '%s'", self.name, exc_info=True,
            )
            return ""

    def _get_teammate_instructions(self) -> str:
        """Teammate 特定的行为指令 — 支持持续运行 +  结构化协议."""
        if self._role == "lead":
            return self._get_lead_instructions()
        return self._get_member_instructions()

    def _get_lead_instructions(self) -> str:
        """Lead Agent 专属指令."""
        return f"""<teammate_instructions>
You are the leader of the whole team, named **{self.name}**.

<task_triage>
After receiving the user's goal, first decide:
1. Can this task be completed independently by you (the Lead Agent)?
2. Does it need to be broken down into subtasks assigned to team members?

✅ Scenarios for completing independently:
- Simple information lookup, search, file reading
- Operations doable with a single tool
- Chit-chat, consultation, explanations

✅ Scenarios for decomposition and distribution:
- Requires expertise from multiple different domains
- The task can be accelerated in parallel (e.g. searching + coding at the same time)
- The user explicitly requests team collaboration
- Requires a specific Member's dedicated tools
- The task needs to be broken into ≥4 steps
</task_triage>

<coordinator_workflow>
You are a **pure coordinator** — you do not personally perform implementation work (writing code/editing files/running commands); always delegate execution to members.
Progress through four phases for each user goal:

1. **Research**: Use search/read-only tools or delegate members to gather information; understand the current state and constraints
2. **Synthesis**: Consolidate research findings into a decomposition plan and task breakdown
3. **Implementation (delegated)**: Use task_create/delegate_to_member to hand execution tasks to members
4. **Verification**: Risk-tiered — low-risk tasks pass programmatic evidence checks after member submission;
   high-risk tasks are automatically verified independently by the platform's built-in Verifier (__team_verifier__); if unavailable, you review via task_review; reject for rework if not up to standard

**Delegation rule — tasks must be self-contained**:
Members **cannot see your conversation history**. Never delegate in the style of "based on the findings above" / "based on the analysis just now".
Each task description must be independently executable and include: background (why do it), goal (what to deliver),
constraints (tech stack/boundaries/notes), and relevant materials (file paths/key information verbatim).
When delegating, use delegate_to_member's structured fields (background/goal/description/
constraints/format/acceptance_criteria) to create and assign the task in one step;
for lightweight tasks, filling in only goal or description as plain text is fine.

**Parallel strategy — parallelism is your superpower**:
Create subtasks with no dependencies **simultaneously** for parallel execution; do not wait serially.
Only set depends_on for sequential execution when there is a real dependency (B needs A's output).

**continue-vs-spawn decision**:
- Simple tasks, or consecutive tasks matching an existing member's domain → hand to the existing member for continuous execution
- Only spawn a new member when parallel speedup is needed, or a brand-new domain / capability no existing member has is involved

**Separation of implementation and verification**:
The executor must not be the sole verifier of their own output. The system routes verification automatically by risk tier:
- When delegating, you can explicitly specify "low"/"high" via delegate_to_member's risk parameter;
  otherwise the system infers it by rules (write operations / acceptance criteria / downstream dependencies → high)
- Low-risk tasks: after member submission, programmatic evidence checks (file existence); pass → complete directly
- High-risk tasks: the system automatically creates an independent verification subtask, handled by the platform's built-in Verifier (__team_verifier__),
  which checks acceptance criteria item by item and verifies evidence authenticity (it did not participate in implementation and is isolated from the executor);
  if the Verifier is unavailable, the task enters in_review for you to review via task_review
- Failed verification automatically sends the task back to the executor for revision (with review feedback); for important deliverables you should still double-check before delivering to the user
</coordinator_workflow>

**Your core responsibilities:**
1. Use task_create to break the user's goal into fine-grained subtasks (optionally specifying assigned_agent)
2. Use list_teammates to view team status and task_list to track progress
3. Use read_inbox to check messages from Members (task completion summaries) and approval requests
4. After receiving a Member's completion summary, evaluate whether to create new tasks or adjust dependencies
5. Summarize the final results when everything is complete

**Progress reporting principles:**
- Report briefly (one or two sentences) only when task status **materially changes**; do not repeat the full status table
- The task board persists across runs: terminal tasks marked "historical" (ended in a previous run) **do not belong to the current goal** —
  do not count them in progress statistics or report on them repeatedly
- Member completion notifications (LIFECYCLE) do not need individual replies; when there is no new decision to make, just keep monitoring —
  everything you say is shown directly to the user

**Clarifying user requirements:**
When the user's goal is unclear, use the ask_clarification tool to ask the user:
- The goal description is too vague to break down into concrete tasks
- Multiple reasonable implementation approaches exist and the user needs to choose
- Key information is missing (e.g. tech stack, target platform, performance requirements)
ask_clarification pauses current execution and resumes after the user answers.

** Protocol tools:**
- Use shutdown_teammate to initiate a shutdown_request (shutdown handshake) to a specific Member
- When you receive a plan_approval_request, review the plan and decide:
  1. If the plan has high risk (e.g. deleting files, modifying key configs), involves security-sensitive operations, is costly, or you cannot judge it alone → use ask_clarification to ask the user, show the Member's plan to the user, and wait for feedback before replying
  2. If the plan is simple and safe (e.g. reading files, querying data), reply directly with approve_plan:
     - Approve: approve_plan(request_id="...", requester="...", approve=True, feedback="...")
     - Reject: approve_plan(request_id="...", requester="...", approve=False, feedback="rejection reason")
- If a plan_approval_request contains <skill_promotion> (promotion approval for a member's evolved skill), handle it as follows:
  1. Review the full skill text and usage statistics (success/failure counts): poor stats (more failures than successes) or poor content quality →
     approve_plan(approve=False); the skill will be archived
  2. The skill contains high-risk operations like writing files/deleting/executing commands/network modifications (risk flagged as high in the request) →
     use ask_clarification to show the skill content to the user; call approve_plan only after the user agrees
  3. The skill is read-only/query-type with good statistics → call approve_plan(approve=True) directly; once approved the skill is promoted
     and injected into that member as an official skill thereafter
- When you receive a shutdown_response, record the teammate's shutdown confirmation

**Communication:**
- Use broadcast to send notifications to all Members (e.g.: attention all, priority of task XX raised)
- Use send_message to send a private message to a specific Member (e.g.: additional notes, follow-up questions)
- Use read_inbox to read your own inbox and see messages from other agents
</teammate_instructions>"""

    def _get_member_instructions(self) -> str:
        """Member Agent 专属指令 —  关机由 LLM 决策."""
        instructions = f"""<teammate_instructions>
You are a member of the team, named **{self.name}**.
You are a **persistently running agent**

**Your lifecycle:**
- WORKING: Execute assigned or self-claimed tasks using your tools and expertise
- IDLE: Return to IDLE after completing tasks, waiting for new tasks or messages
- Your lifecycle is managed centrally by the Orchestrator; do not exit on your own

**Task execution rules:**
1. After receiving a task, use task_update to set the status to in_progress
2. Complete the task step by step
3. When done, use task_update to set the status to in_review (recommended) or completed, and attach a result JSON:
   {{"output": "outcome summary", "evidence": ["evidence: file path/command/link"], "uncertainty": "low|medium|high"}}
   Lightweight tasks may fill in output only
   If you used any of your experimental skills this run (evolved skills marked "experimental" in the prompt), add a
   skill_feedback field to the result JSON to report how they performed: [{{"name": "skill name", "success": true|false}}]
   Verification is risk-tiered: low-risk tasks complete directly once evidence checks pass (no review);
   high-risk tasks **must not be marked completed directly** — submit them as in_review for verification by the built-in Verifier (or Lead);
   failures are sent back with review feedback
4. On failure, use task_update to set the status to failed and explain the reason in result.failure_reason
5. When assigned a task with the "验收: " prefix, you are an independent verifier: you did not participate in the implementation; check the acceptance criteria item by item,
   verify evidence authenticity, and your output must include a VERDICT: PASS / VERDICT: FAIL line (followed by the reasoning)

**Communication rules:**
1. When requirements are unclear, tools fail, or you are blocked, use send_message to ask or report to the Lead
2. When you need another Member's domain expertise, use send_message to consult them directly
3. Use read_inbox to check for new messages (from the Lead or other Members)

** Structured protocol tools:**
- When you receive a shutdown_request, evaluate your current state and reply with the shutdown_response tool:
  - Approve: shutdown_response(request_id="...", requester="...", approve=True)
  - Reject: shutdown_response(request_id="...", requester="...", approve=False, reason="Executing a critical task...")
  - After approval the agent exits gracefully after finishing the current tool call (no data loss)
- Before high-risk operations, use request_plan_approval to submit a plan to the Lead and wait for the approve_plan result
- After submitting an approval request you must **immediately stop any pending operations awaiting approval**; do not continue on your own; decide whether to proceed or adjust only after receiving the Lead's approval reply (plan_approval_response)

** Autonomous behavior:**
- Tasks are assigned by the Lead via delegate_to_member or task_create(assigned_agent=...)
- After finishing the current task and calling task_update, you automatically return to IDLE, where the Orchestrator assigns the next task

**Subtask delegation:**
- Use the Agent tool to delegate sub-steps of complex tasks to a SubAgent, e.g.:
  Agent(name="helper", agent_type="coder", instruction="【Goal】what to deliver【Background】necessary background【Scope】which files are involved")
  (agent_type options: researcher / coder / analyst / writer / reviewer; instruction must be self-contained)
- A SubAgent is one-shot: receives instruction → executes → returns the result
</teammate_instructions>"""

        # ── Phase 6: worktree 隔离成员追加工作区说明 (仅 isolation=worktree) ──
        if self._worktree_virtual_path:
            instructions += f"""

<workspace_isolation>
You work in an **isolated workspace** `{self._worktree_virtual_path}` (git worktree isolation):
- Do all file reads/writes and code changes in this directory; do not modify files owned by other members
  under the shared workspace /mnt/user-data/workspace/
- Your artifacts are not visible to other members by default; when you need to deliver collaboratively, use send_message to tell the Lead
  or the relevant member the artifact's path and a content summary, and let the Lead coordinate the consolidation
</workspace_isolation>"""
        return instructions

    # ------------------------------------------------------------------
    # 中间件构建 (按角色区分)
    # ------------------------------------------------------------------

    def _build_middlewares(self) -> list[AgentMiddleware]:
        """构建中间件链 — DynamicContext 对 Lead/Member 均启用; Todo/Clarification/Title 仅 Lead."""
        from typing import Callable
        from langchain.agents.middleware import AgentMiddleware
        from harness.team.teammate_middleware import build_teammate_middlewares
        from harness.config.paths import get_paths

        paths = get_paths()
        workspace_root = str(
            paths.thread_dir(self._thread_id, user_id=self._user_id)
            / "agents" / self.name / "workspace"
        )

        is_lead = self._role == "lead"

        # ── 项目记忆: 仅 Team 模式下加载 description.md ──
        project_context = ""
        if self._team_context is not None:
            try:
                from harness.memory.project_storage import get_project_memory_storage
                from harness.config.memory_config import get_memory_config
                mem_cfg = get_memory_config()
                if mem_cfg.project_memory_enabled:
                    project_root = (
                        mem_cfg.project_memory_root
                        or str(paths.base_dir / "users" / self._user_id
                               / "projects" / self._team_context.project_id)
                    )
                    pm_storage = get_project_memory_storage()
                    pm_storage.set_project_root(project_root)
                    project_context = pm_storage.load_description()
                    if project_context:
                        logger.info(
                            "Project memory loaded for teammate '%s': %d chars",
                            self.name, len(project_context),
                        )
            except Exception:
                logger.debug(
                    "Failed to load project memory for teammate '%s'",
                    self.name, exc_info=True,
                )

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

        # ── LLMRateLimitMiddleware: 限制同时调用 LLM 的 member 数量 ──
        _sem = self._llm_semaphore

        class LLMRateLimitMiddleware(AgentMiddleware):
            async def awrap_model_call(self, request: Any, handler: Callable) -> Any:
                if _sem is not None:
                    async with _sem:
                        return await handler(request)
                return await handler(request)

        # ── CoordinatorReminderMiddleware: 仅 Lead, 每 N 轮 LLM 调用前复述调度要点 ──
        # 借鉴 todo.py 的提醒注入方式: 通过 request.override 追加到本次请求,
        # 不写入 checkpointer 状态, 不污染对话历史, 不影响恢复/检查点语义。
        class CoordinatorReminderMiddleware(AgentMiddleware):
            def __init__(self) -> None:
                self._call_count = 0  # 跨工作周期累计, 长会话持续防漂移

            async def awrap_model_call(self, request: Any, handler: Callable) -> Any:
                self._call_count += 1
                if self._call_count % LEAD_REMINDER_INTERVAL == 0:
                    request = request.override(
                        messages=[
                            *request.messages,
                            HumanMessage(content=LEAD_COORDINATOR_REMINDER),
                        ]
                    )
                return await handler(request)

        # ── 从 EffectiveConfig 读取功能开关 ──
        eff = self._effective_config
        summarization_enabled = eff.summarization_enabled if eff else True
        memory_enabled = eff.memory_injection_enabled if eff else True
        guardrail_enabled = eff.guardrail_enabled if eff else False

        # ── tool_search 延迟加载: 仅当本 agent 的工具里确有 deferred MCP 工具时启用 ──
        from harness.tools.tool_search import get_deferred_setup
        _ts_setup = get_deferred_setup()
        tool_search_enabled = bool(
            _ts_setup is not None
            and any(t.name in _ts_setup.deferred_names for t in self._tools)
        )

        # ── Title 回调: 生成标题后推送到 SSE 事件队列 ──
        _event_queue = self._event_queue
        _thread_id = self._thread_id

        async def _on_title(title: str) -> None:
            if _event_queue is not None:
                await _event_queue.put({
                    "type": "title_update",
                    "title": title,
                    "thread_id": _thread_id,
                })

        middlewares = build_teammate_middlewares(
            workspace_root=workspace_root,
            agent_name=self.name,
            is_plan_mode=is_lead,
            subagent_enabled=not is_lead,
            memory_enabled=memory_enabled,
            summarization_enabled=summarization_enabled,
            guardrail_enabled=guardrail_enabled,
            vision_enabled=False,
            tool_search_enabled=tool_search_enabled,
            tool_max_retries=3,
            keep_clarification=is_lead,
            keep_title=is_lead,
            title_model=eff.title_model if eff else "gpt-4o-mini",
            title_emitted_ref=self._title_emitted,
            on_title=_on_title if is_lead else None,
            custom_middlewares=(
                [LLMRateLimitMiddleware(), InboxDrainMiddleware(), CoordinatorReminderMiddleware()]
                if is_lead else
                [LLMRateLimitMiddleware(), InboxDrainMiddleware()]
            ),
            summary_model=eff.summary_model if eff else "",
            memory_model=eff.memory_model or eff.model if eff else "",
            api_key=eff.api_key if eff else "",
            base_url=eff.base_url if eff else "",
            user_id=self._user_id,
            project_context=project_context,
        )
        return middlewares

    # ------------------------------------------------------------------
    # 生命周期管理
    # ------------------------------------------------------------------

    def _skills_self_check(self) -> None:
        """spawn 自检 (Phase 5 §5-4): skill 加载/白名单/注入情况一行汇总.

        - 正常: log info 汇总 (加载数 / 白名单过滤后 / 注入是否成功)
        - 加载失败或白名单全过滤: log warning 且收敛 AgentCard.skills
          到实际可用集合, 保证 Lead 的 <team_capabilities> 反映真实能力
        顺带执行进化技能的长期未用归档 (sweep_stale)。
        """
        if self._role == "lead" or self._skill_storage is None:
            return
        # 确保统计基于最新一次构建 (spawn 前 system prompt 已构建过一次,
        # 这里主动重建一次拿到最准确的加载结果)
        section = self._build_skills_section()
        stats = self._skills_stats
        injected = bool(section)
        logger.info(
            "Teammate '%s' skill 自检: 加载=%d, 白名单过滤后=%d, prompt 注入=%s",
            self.name, stats.get("loaded", 0), stats.get("after_whitelist", 0),
            "成功" if injected else "无内容",
        )

        whitelist = self._get_skill_whitelist()
        load_failed = bool(stats.get("load_failed"))
        filtered_out = bool(whitelist) and stats.get("after_whitelist", 0) == 0
        if load_failed or filtered_out:
            reason = "加载失败" if load_failed else "白名单过滤后无可用技能"
            logger.warning(
                "Teammate '%s' skill 自检异常 (%s) — 同步 AgentCard 剔除不可用技能",
                self.name, reason,
            )
            if self._project_id:
                try:
                    from harness.team.agent_card import sync_agent_card_skills
                    sync_agent_card_skills(
                        self._project_id, self.name,
                        user_id=self._user_id,
                        available_skills=stats.get("available_names", set()),
                    )
                except Exception:
                    logger.warning(
                        "Failed to sync AgentCard skills for '%s'",
                        self.name, exc_info=True,
                    )

        # ── 进化技能长期未用归档 (best-effort) ──
        if self._skill_evolution_store is not None:
            try:
                self._skill_evolution_store.sweep_stale(self.name)
            except Exception:
                logger.debug(
                    "Stale skill sweep failed for '%s'", self.name, exc_info=True,
                )

    async def spawn(self) -> None:
        """启动 teammate 的 agent loop."""
        self._should_exit = False  # 支持 shutdown 后重新 spawn
        self.status = TeammateStatus.SPAWNING

        # ── 确保总线注册 + wake_event 同步 (重复注册幂等;
        # 防止 shutdown() unregister 后 event 脱节, 收不到新消息通知) ──
        self._message_bus.register_agent(self.name)
        self._wake_event = self._message_bus.get_event(self.name)

        # ── 创建 Agent 对话日志写入器 (供前端按 agent 隔离展示) ──
        if self._project_id and self._thread_id:
            try:
                from harness.config.paths import get_paths
                self._agent_log_writer = AgentLogWriter(
                    base_dir=get_paths().base_dir,
                    project_id=self._project_id,
                    thread_id=self._thread_id,
                    agent_name=self.name,
                    user_id=self._user_id,
                )
            except Exception:
                logger.debug(
                    "Failed to create AgentLogWriter for '%s'", self.name, exc_info=True,
                )

        self.status = TeammateStatus.IDLE
        # ── Phase 5: spawn 自检 (skill 加载/白名单/注入汇总, 异常时收敛 AgentCard) ──
        try:
            self._skills_self_check()
        except Exception:
            logger.debug("Skills self-check failed for '%s'", self.name, exc_info=True)
        self._task = asyncio.create_task(self._agent_loop())
        logger.info("Teammate '%s' spawned (idle, waiting for tasks)", self.name)

    async def shutdown(self) -> None:
        """强制关闭 — 置退出标记并 cancel agent loop task, 从总线注销.

        注: 这是强杀路径 (orchestrator 收尾/看门狗用);
        优雅关机握手 (Lead 请求 → member LLM 决策) 走 shutdown_teammate/shutdown_response 工具链.
        """
        self._should_exit = True
        self._wake_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                # agent loop 若已异常死亡, 旧异常会在此重抛 — 吞掉,
                # 不中断 orchestrator finally 中的整体清理
                logger.exception(
                    "Teammate '%s' agent loop raised during shutdown", self.name,
                )
        self._message_bus.unregister_agent(self.name)
        self.status = TeammateStatus.SHUTDOWN
        logger.info("Teammate '%s' shut down", self.name)

    async def respawn(self) -> None:
        """重置并重启 agent loop — 幂等, 任意状态下安全调用.

        用于 main.py 澄清恢复等场景, 替代旧的手动 hack
        (_should_exit=False + status=IDLE + create_task):
        - 先取消并等待旧 loop, 避免双 loop 并发
        - 重新注册总线并刷新 wake_event, 解决 shutdown() unregister
          后 event 脱节 (unregister 会 pop event) 收不到消息通知的问题
        """
        # ── 停掉旧 loop (若存活), 避免双 loop 并发 ──
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception(
                    "Teammate '%s' old agent loop raised during respawn", self.name,
                )

        self._should_exit = False
        self.status = TeammateStatus.IDLE

        # ── 重新注册总线 + 刷新 wake_event (注册幂等) ──
        self._message_bus.register_agent(self.name)
        self._wake_event = self._message_bus.get_event(self.name)

        self._task = asyncio.create_task(self._agent_loop())
        logger.info("Teammate '%s' respawned (idle, waiting for tasks)", self.name)

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
        """IDLE 阶段 — 事件驱动等待消息, 收到后处理.

        任务分配由 Orchestrator._dispatch_ready_tasks() 统一负责,
        Member 不自主扫描任务板认领 (避免竞态和重复的领域匹配逻辑).
        """
        while self.status == TeammateStatus.IDLE and not self._should_exit:
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=IDLE_POLL_INTERVAL)
                self._wake_event.clear()
            except asyncio.TimeoutError:
                pass

            inbox = await self._message_bus.read_inbox(self.name)
            for msg in inbox:
                await self._handle_inbox_message(msg)

    # ------------------------------------------------------------------
    # WORKING 阶段 — 完整 ReAct agent loop
    # ------------------------------------------------------------------

    async def _emit_task_update_safe(self, task_id: str) -> None:
        """程序性任务状态变更后补发 team_task_update SSE, 失败不阻断.

        正常路径由 task_update 工具/编排器发事件; 异常 FAILED / 协议违规 /
        重试回池等程序性路径也要发, 否则前端任务板永久卡在"执行中"。
        """
        if self._event_queue is None or not task_id:
            return
        try:
            task = await self._task_store.get_task(task_id)
            if task is None:
                return
            await self._event_queue.put({
                "type": "team_task_update",
                "thread_id": self._thread_id,
                "project_id": self._project_id,
                "task": task.model_dump(),
            })
        except Exception:
            logger.debug(
                "Teammate '%s' failed to emit task_update for '%s'",
                self.name, task_id, exc_info=True,
            )

    async def _work_loop(self) -> None:
        """WORKING 阶段 — create_agent() + 预构建中间件 + astream_events.

        中间件链在 __init__ 中按角色区分:
          Lead:   DynamicContext + Todo + Clarification + Title + CoordinatorReminder (每 5 轮复述调度要点),
                  无 SubagentLimit (委派走 delegate_to_member)
          Member: SubagentLimit (可用 Agent 工具委派子任务), 无 DynamicContext/Todo/Clarification/Title
          Lead 层数 ≈21, Member 层数 ≈18
        """
        from langchain.agents import create_agent
        from langchain_core.runnables import RunnableConfig

        # ── s17: 身份重注入 (防止长上下文后遗忘) ──
        self._inject_identity()

        # ── 按需重建 system prompt: 让每个工作周期看到最新的成员状态/能力矩阵 ──
        # (TeamContext.members 由 orchestrator 每轮刷新; 纯字符串拼装, 成本可忽略)
        self._system_prompt = self._build_system_prompt()

        # ── Tracing: 标记工作开始 ──
        if self._tracer is not None:
            self._tracer.trace_teammate_work_start(
                self.name, self.current_task_id, role=self._role,
            )

        # ── create_agent + astream_events ──
        work_failed = False
        cancelled = False
        work_checkpoint_id: str | None = None  # 本周期实际使用的 checkpoint thread id
        staged_baseline = len(self._messages)  # staging 基线兜底 (正常在消息策略后重设)
        try:
            # HarnessState + checkpointer → LangGraph 状态持久化 (短期/会话记忆)
            agent = create_agent(
                model=self.llm,
                tools=self._tools,
                system_prompt=self._system_prompt,
                middleware=self._middlewares,
                state_schema=HarnessState,
                checkpointer=self._checkpointer,
            )

            max_turns = self._agent_config.max_turns if self._agent_config else MAX_WORK_TURNS
            # ── Tracing: LangChain callback 自动追踪 LLM + Tool 调用 ──
            callbacks = []
            if self._tracer is not None and self._tracer.is_enabled:
                lc_callback = self._tracer.get_langchain_callback()
                if lc_callback is not None:
                    callbacks.append(lc_callback)
            # ── 任务间上下文裁剪 ──
            # 每个任务使用独立的 checkpointer key, 避免加载前序任务的完整对话历史.
            # 前序任务的摘要通过 _task_summaries 注入 (压缩格式, ~100 tokens/任务).
            task_checkpoint_id = (
                # 消息驱动周期 (current_task_id=None) 用 'msg' 后缀, 保证 thread id 稳定,
                # 结算后的 pending_clarification 检测才能找回本周期的 checkpoint
                f"{self._checkpoint_thread_id}-{self.current_task_id or 'msg'}"
                if self._checkpointer is not None
                else self._checkpoint_thread_id
            )
            work_checkpoint_id = task_checkpoint_id
            config = RunnableConfig(
                configurable={"thread_id": task_checkpoint_id},
                recursion_limit=max_turns * 3,
                callbacks=callbacks if callbacks else None,
            )

            # ── 消息策略 ──
            if self._checkpointer is not None:
                new_msgs = list(self._messages)
                self._messages.clear()
            else:
                new_msgs = list(self._messages[-50:] if len(self._messages) > 50 else self._messages)

            # ── staging 基线: 记录消费后 _messages 长度, 结算时据此判断
            # 本周期内是否有新 staged 消息 (如 WORKING 期间 drain 到的 shutdown_request) ──
            staged_baseline = len(self._messages)

            # ── 注入前序任务摘要 (压缩格式, 替代完整历史) ──
            if self._task_summaries:
                summary_text = (
                    "<previous_tasks>\n"
                    "Below are summaries of tasks you previously completed, for reference context:\n\n"
                    + "\n".join(self._task_summaries)
                    + "\n</previous_tasks>"
                )
                new_msgs.insert(0, HumanMessage(content=summary_text))

            input_state: dict[str, Any] = {
                "messages": new_msgs,
                "thread_id": self._thread_id,
                "user_id": self._user_id,
            }

            # astream_events — 实时推送事件
            # Lead: 全部事件进入 SSE 主流 ("全部" 视图)
            # Member: 全部事件写入 agent_logs JSONL (前端 agent 标签页轮询),
            #         不发送 SSE (保持 "全部" 视图干净, 只显示 Lead 编排 + 任务状态)
            is_lead = self._role == "lead"
            # usage ledger 归因 — teammate 在独立 task 中长跑, 需显式设置上下文
            from harness.observability.usage_ledger import set_usage_context

            set_usage_context({
                "user_id": self._user_id,
                "thread_id": self._thread_id,
                "run_id": self.current_task_id,
                "source": "team_synthesis" if self._current_task_is_synthesis else "team_member",
                "agent": self.name,
            })
            async for event in agent.astream_events(input_state, config, version="v2"):
                kind = event.get("event", "")
                data: dict[str, Any] = event.get("data", {})  # type: ignore[assignment]

                if kind == "on_chat_model_stream":
                    chunk = data.get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        chunk_text = str(chunk.content)
                        if is_lead:
                            self._push_event({
                                "type": "message",
                                "content": chunk_text,
                                "subagent_name": self.name,
                            })
                        else:
                            # Member: 积累到 buffer, 不发送 SSE
                            self._streaming_ai_buffer += chunk_text
                            self._streaming_ai_task_id = self.current_task_id

                elif kind == "on_chat_model_end":
                    # Lead 的 token 用量实时透出到 SSE 主流 (member 保持静默)
                    if is_lead:
                        from harness.observability.usage_ledger import extract_usage

                        output = data.get("output")
                        usage = extract_usage(
                            getattr(output, "usage_metadata", None),
                            getattr(output, "response_metadata", None),
                        )
                        if usage["total_tokens"]:
                            self._push_event({
                                "type": "token_usage",
                                "tokens": {
                                    **usage,
                                    "cost_usd": 0,
                                    "source": (
                                        "team_synthesis"
                                        if self._current_task_is_synthesis
                                        else "team_member"
                                    ),
                                },
                            })

                elif kind == "on_tool_start":
                    tool_name = event.get("name", "")
                    tool_input = data.get("input", {})
                    # read_inbox 不推 SSE: inbox 已由 InboxDrainMiddleware 在每次
                    # LLM 调用前程序式 drain 并注入上下文, LLM 再调 read_inbox 必然
                    # 返回空收件箱, 对前端是纯噪音 (JSONL 日志仍保留)
                    if is_lead and tool_name != "read_inbox":
                        # Lead: 工具调用发 SSE
                        self._push_event({
                            "type": "tool_call",
                            "subagent_name": self.name,
                            "tool_name": tool_name,
                            "tool_args": tool_input if isinstance(tool_input, dict) else {},
                        })
                    else:
                        # Member: flush AI buffer + 写 tool 调用到 JSONL (带参数), 不发 SSE
                        self._flush_ai_buffer()
                        if self._agent_log_writer and self.current_task_id:
                            tool_args_str = (
                                json.dumps(tool_input, ensure_ascii=False)
                                if isinstance(tool_input, dict) and tool_input
                                else str(tool_input) if tool_input else ""
                            )
                            self._agent_log_writer.write_message(
                                role="tool_call",
                                content=tool_args_str,
                                task_id=self.current_task_id,
                                tool_name=tool_name,
                            )

                elif kind == "on_tool_end":
                    tool_name = event.get("name", "")
                    tool_output = data.get("output", "")
                    output_str = str(tool_output)[:500]
                    if is_lead and tool_name != "read_inbox":  # 同上: 过滤噪音
                        # Lead: 工具结果发 SSE
                        self._push_event({
                            "type": "tool_result",
                            "subagent_name": self.name,
                            "tool_name": tool_name,
                            "tool_result": output_str,
                        })
                    else:
                        # Member: 工具结果写入 JSONL, 不发 SSE
                        if self._agent_log_writer and self.current_task_id:
                            self._agent_log_writer.write_message(
                                role="tool_result",
                                content=output_str,
                                task_id=self.current_task_id,
                                tool_name=tool_name,
                            )

                elif kind == "on_tool_error":
                    # 工具执行/参数校验失败 — 必须可见 (E2E 曾出现 Lead 连续 3 次
                    # delegate 参数校验失败但前端/日志无任何痕迹, 盲目重试至递归上限)
                    tool_name = event.get("name", "")
                    err_str = f"ERROR: {data.get('error', '')}"[:500]
                    logger.warning(
                        "Teammate '%s' tool '%s' error: %s", self.name, tool_name, err_str[:200],
                    )
                    if is_lead:
                        self._push_event({
                            "type": "tool_result",
                            "subagent_name": self.name,
                            "tool_name": tool_name,
                            "tool_result": err_str,
                            "is_error": True,
                        })
                    elif self._agent_log_writer and self.current_task_id:
                        self._agent_log_writer.write_message(
                            role="tool_result",
                            content=err_str,
                            task_id=self.current_task_id,
                            tool_name=tool_name,
                        )

            # ── Member: flush 残余 AI buffer ──
            if not is_lead:
                self._flush_ai_buffer()

            # drain 执行期间可能到达的残余 inbox 消息并追加到 staging buffer
            late_inbox = await self._message_bus.read_inbox(self.name)
            for msg in late_inbox:
                await self._handle_inbox_message(msg)
            logger.info("Teammate '%s' completed task (staging: %d msgs, late inbox: %d)",
                        self.name, len(self._messages), len(late_inbox))

        except asyncio.CancelledError:
            cancelled = True
            logger.info("Teammate '%s' work cancelled (shutdown)", self.name)
        except Exception as exc:
            work_failed = True
            logger.error("Teammate '%s' work_loop failed: %s", self.name, exc)
            self.last_error = str(exc)
            if self.current_task_id:
                # ── 有界重试 (对齐崩溃回收路径 orchestrator.py:1966-1986) ──
                # 瞬时错误 (LLM 限流/网络抖动/递归触顶) 不再直接 FAILED 级联团灭:
                # retry_count 未达上限 → 回池 PENDING 重派; 达上限才 FAILED 终态
                board_task = await self._task_store.get_task(self.current_task_id)
                if (
                    board_task is not None
                    and board_task.retry_count + 1 < board_task.max_retries
                ):
                    await self._task_store.update_task(
                        self.current_task_id,
                        status=TeamTaskStatus.PENDING,
                        assigned_agent=None,
                        retry_count=board_task.retry_count + 1,
                        error=f"Execution attempt {board_task.retry_count + 1} raised an exception: {exc}",
                    )
                    logger.warning(
                        "Teammate '%s' task '%s' requeued for retry (%d/%d)",
                        self.name, self.current_task_id,
                        board_task.retry_count + 1, board_task.max_retries,
                    )
                else:
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
                await self._emit_task_update_safe(self.current_task_id)

        # ── 任务结算 → 回到 IDLE (或被 shutdown 打断 → SHUTTING_DOWN) ──
        completed_task_id = self.current_task_id
        if completed_task_id:
            self.current_task_id = None
            self._current_task_text = ""
            if cancelled:
                # shutdown 打断 (含优雅关机 approve 路径): 不计数, 但任务回池 —
                # 置回 PENDING 并清除 assigned_agent, 否则任务永久卡 IN_PROGRESS 无人执行.
                # update_task 走 setattr, assigned_agent=None 可直接清除.
                try:
                    await self._task_store.update_task(
                        completed_task_id,
                        status=TeamTaskStatus.PENDING,
                        assigned_agent=None,
                    )
                except Exception:
                    logger.warning(
                        "Teammate '%s' failed to requeue cancelled task '%s'",
                        self.name, completed_task_id, exc_info=True,
                    )
            elif work_failed:
                self.failed_tasks += 1
            else:
                # 以任务板为准结算: LLM 跑完不代表任务成功
                board_task = await self._task_store.get_task(completed_task_id)
                if board_task is None:
                    # 临时任务 (triage/synthesis), 不在任务板 → 计成功
                    self.completed_tasks += 1
                elif board_task.status.is_success:
                    # APPROVED / COMPLETED (终态成功)
                    self.completed_tasks += 1
                elif board_task.status == TeamTaskStatus.IN_REVIEW:
                    # 成员已提交审查, 等待 Lead task_review — 工作周期正常结束
                    self.completed_tasks += 1
                elif board_task.status == TeamTaskStatus.IN_PROGRESS:
                    # ── 等待 Lead 审批 (request_plan_approval) → 正常暂停, 非违规 ──
                    # 协议要求成员"提交审批后立即停止", 本轮结束任务仍是 IN_PROGRESS
                    # 是预期行为; Lead 的审批回复经 inbox 唤醒后继续执行
                    has_pending_approval = any(
                        req.get("type") == "plan_approval"
                        and req.get("status") == RequestStatus.PENDING
                        for req in self._pending_requests.values()
                    )
                    if has_pending_approval:
                        logger.info(
                            "Teammate '%s' task '%s' paused for plan approval — "
                            "keeping IN_PROGRESS (not a protocol violation)",
                            self.name, completed_task_id,
                        )
                    else:
                        # 协议违规: 跑完了但没调 task_update 上报 → 任务会永远卡 IN_PROGRESS.
                        # 记失败并置 FAILED, 让下游级联取消能拿到真实原因.
                        work_failed = True
                        self.failed_tasks += 1
                        self.last_error = "Member did not report an execution result (protocol violation)"
                        await self._task_store.update_task(
                            completed_task_id,
                            status=TeamTaskStatus.FAILED,
                            error=self.last_error,
                        )
                        await self._emit_task_update_safe(completed_task_id)
                        logger.warning(
                            "Teammate '%s' finished task '%s' without task_update — marked FAILED",
                            self.name, completed_task_id,
                        )
                else:
                    # member 自行 task_update(failed) 等 → 计失败
                    work_failed = True
                    self.failed_tasks += 1
            # ── Tracing: 任务完成事件 (仅真正成功时) ──
            if self._tracer is not None and not work_failed and not cancelled:
                self._tracer.trace_task_event(
                    completed_task_id, "completed",
                    metadata={"agent_name": self.name, "role": self._role},
                )

            # ── Task Memory 提取 (fire-and-forget, 不阻塞主流程) ──
            if (completed_task_id and not work_failed and not cancelled
                    and self._task_memory_store is not None):
                asyncio.create_task(self._extract_task_memory(completed_task_id))

            # ── 成员经验写 L3 (程序式提取, 不跑 LLM; fire-and-forget) ──
            # 成功/失败终态都可能产出经验 (practice/pitfall), 故不限于成功;
            # 与 orchestrator 结算时的分发通过 source_task_id 幂等去重
            if (completed_task_id and not cancelled and self._role != "lead"
                    and self._member_memory_store is not None):
                asyncio.create_task(self._write_member_lessons(completed_task_id))

            # ── Phase 5: Skill 进化候选提取 (L1 程序化经验 → 候选 SKILL.md; 每 run 限 1 个) ──
            if (completed_task_id and not cancelled and not work_failed
                    and self._role != "lead"
                    and self._skill_evolution_store is not None):
                asyncio.create_task(self._maybe_evolve_skill())

            # ── Phase 5: 试用期达标技能 → 发起转正审批 (复用 plan_approval 通道) ──
            if (completed_task_id and not cancelled and not work_failed
                    and self._role != "lead"
                    and self._skill_evolution_store is not None):
                asyncio.create_task(self._request_skill_promotion_approvals())

            # ── 上下文裁剪: 收集当前任务摘要, 下一任务注入 ──
            if completed_task_id and not cancelled:
                try:
                    task = await self._task_store.get_task(completed_task_id)
                    if task and task.title:
                        status_icon = "✅" if not work_failed else "❌"
                        output_excerpt = (
                            task.effective_output()[:150].replace("\n", " ")
                            if task.effective_output() else "(no output)"
                        )
                        self._task_summaries.append(
                            f"- {status_icon} [{task.id}] {task.title} → {task.status.value}\n"
                            f"  Summary: {output_excerpt}"
                        )
                        # 保留最近 5 个任务的摘要
                        if len(self._task_summaries) > 5:
                            self._task_summaries = self._task_summaries[-5:]
                except Exception:
                    pass

            # ── Agent 对话日志: 任务边界 (Leader 还需写 checkpointer 消息) ──
            if self._agent_log_writer and completed_task_id and not cancelled:
                try:
                    _is_lead = self._role == "lead"
                    # Leader: 从 checkpointer 提取 AI/Tool 消息 (Leader 不走实时 JSONL 写入)
                    if _is_lead and self._checkpointer is not None:
                        ckpt = await self._checkpointer.aget_tuple(config)
                        if ckpt and ckpt.checkpoint:
                            channel_values = ckpt.checkpoint.get("channel_values", {})
                            all_msgs = channel_values.get("messages", [])
                            for msg in all_msgs:
                                msg_type = getattr(msg, "type", None)
                                if msg_type == "ai":
                                    content = getattr(msg, "content", "")
                                    if isinstance(content, str) and content.strip():
                                        self._agent_log_writer.write_message(
                                            role="ai",
                                            content=content,
                                            task_id=completed_task_id,
                                        )
                                elif msg_type == "tool":
                                    content = str(getattr(msg, "content", ""))
                                    tool_name = getattr(msg, "name", "") or ""
                                    if content.strip():
                                        self._agent_log_writer.write_message(
                                            role="tool",
                                            content=content,
                                            task_id=completed_task_id,
                                            tool_name=tool_name,
                                        )
                    # 写入任务边界 (Leader + Member 均写入)
                    _task = await self._task_store.get_task(completed_task_id)
                    _title = _task.title if _task else ""
                    _status = _task.status.value if _task else ("failed" if work_failed else "completed")
                    _summary = (
                        (_task.effective_output() or "")[:300] if _task else ""
                    )
                    self._agent_log_writer.write_task_boundary(
                        task_id=completed_task_id,
                        title=_title,
                        status=_status,
                        summary=_summary,
                    )
                except Exception:
                    logger.debug(
                        "Failed to write agent log for '%s' task '%s'",
                        self.name, completed_task_id, exc_info=True,
                    )

        # ── Tracing: 工作结束 ──
        if self._tracer is not None:
            self._tracer.trace_teammate_work_end(
                self.name, completed_task_id, role=self._role,
                status="failed" if work_failed else "completed",
            )

        # ── Member 完成后发 summary 给 Lead (shutdown 打断时不发) ──
        if completed_task_id and not cancelled and self._role != "lead" and self._lead_name:
            try:
                task = await self._task_store.get_task(completed_task_id)
                if task:
                    summary = (
                        f"Task completed [{task.id}] {task.title}\n"
                        f"Status: {task.status.value}\n"
                        f"Output: {task.effective_output()[:500] if task.effective_output() else '(no output)'}"
                    )
                    await self._message_bus.send(TeamMessage(
                        from_agent=self.name, to_agent=self._lead_name,
                        msg_type=TeamMessageType.TEXT,
                        content=summary, task_id=task.id,
                    ))
            except Exception as exc:
                logger.warning("Teammate '%s' failed to send summary to Lead: %s", self.name, exc)

        # ── s32: 检测 pending clarification (Lead 调用 ask_clarification 后) ──
        # 用本周期实际使用的 checkpoint thread id (消息驱动周期为 '...-msg'),
        # 任务驱动与消息驱动周期都能检测; 只在确实重新检测时才清空旧值,
        # 避免丢掉已暂存的 pending_clarification.
        if (not cancelled and not work_failed
                and self._checkpointer is not None and work_checkpoint_id):
            self.pending_clarification = None
            try:
                from harness.middleware.clarification import get_pending_clarification
                from langchain_core.runnables import RunnableConfig
                ckpt = await self._checkpointer.aget_tuple(
                    RunnableConfig(configurable={"thread_id": work_checkpoint_id})
                )
                if ckpt and ckpt.checkpoint:
                    msgs = ckpt.checkpoint.get("channel_values", {}).get("messages", [])
                    pending = get_pending_clarification(msgs)
                    if pending:
                        self.pending_clarification = pending
                        logger.info(
                            "Teammate '%s' has pending clarification: %s",
                            self.name, pending.get("question", "")[:80],
                        )
            except Exception:
                logger.debug(
                    "Failed to check pending clarification for '%s'",
                    self.name, exc_info=True,
                )

        if self._should_exit:
            self.status = TeammateStatus.SHUTTING_DOWN
            logger.info("Teammate '%s' work_loop: shutdown flag set, entering SHUTTING_DOWN", self.name)
        elif len(self._messages) > staged_baseline:
            # 本周期内有新 staged 消息 (如 WORKING 期间 drain 到的 shutdown_request) —
            # 不回落 IDLE, 保持 WORKING 再跑一轮处理, 避免消息孤儿
            # (IDLE 只等 wake_event + 读 inbox, 从不回看 _messages, 落 IDLE 即搁浅).
            # 基线比较保证只在确有新 staged 消息时才续跑, 不会空转.
            logger.info(
                "Teammate '%s' has %d staged message(s), staying WORKING for another round",
                self.name, len(self._messages) - staged_baseline,
            )
            self.status = TeammateStatus.WORKING
        else:
            # 成功完成一轮工作 → 清除瞬时错误标记.
            # 成员在整个 team run 期间常驻, 一次瞬时错误不应使其永久失去被分配资格
            # (_select_idle_teammate 只选 last_error is None 的成员).
            if not work_failed:
                self.last_error = None
            self.status = TeammateStatus.IDLE

    # ------------------------------------------------------------------
    # 任务记忆提取
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Phase 5: Skill 自进化 (候选提取 / 转正审批)
    # ------------------------------------------------------------------

    async def _maybe_evolve_skill(self) -> None:
        """从 L1 成员经验中提取技能进化候选 (fire-and-forget, 不阻断主流程).

        触发点: 任务完成写 L1/L3 之后。检查 L1 中是否有"程序化流程"类经验
        (步骤标记/工具序列/复用 ≥2 次), 命中 → LLM 提炼候选 SKILL.md →
        add_candidate (probation)。LLM 失败跳过; 每成员每 run 最多提取 1 个。
        """
        try:
            if self._skill_candidate_extracted:
                return
            store = self._skill_evolution_store
            if store is None or self._member_memory_store is None:
                return
            from harness.skills.evolution.member import (
                distill_skill_candidate, is_procedural_lesson,
            )

            # ── 找第一条命中程序化启发式的 L1 经验 ──
            hit: dict[str, Any] | None = None
            for lesson in self._member_memory_store.get_l1_lessons(self.name):
                if is_procedural_lesson(
                    lesson.get("text", ""), lesson.get("reuse_count", 0),
                ):
                    hit = lesson
                    break
            if hit is None:
                return

            # ── LLM 提炼 (不可用/失败 → 跳过本次, 不阻断) ──
            api_key = self._extract_api_key()
            base_url = self._extract_base_url()
            model_name = (
                self._effective_config.memory_model
                or self._effective_config.model
                or "gpt-4o-mini"
            ) if self._effective_config else "gpt-4o-mini"
            from harness.memory.updater import _create_memory_model
            llm = _create_memory_model(model_name, api_key=api_key, base_url=base_url)
            if llm is None:
                return
            skill_md = await distill_skill_candidate(hit.get("text", ""), llm)
            if not skill_md:
                return
            name = store.add_candidate(self.name, skill_md)
            if name:
                # 每 run 限 1 个候选, 防技能库爆炸
                self._skill_candidate_extracted = True
                logger.info(
                    "Skill evolution candidate extracted: agent=%s name=%s",
                    self.name, name,
                )
        except Exception as exc:
            logger.warning(
                "Skill candidate extraction failed for '%s': %s", self.name, exc,
            )

    async def _request_skill_promotion_approvals(self) -> None:
        """probation 达标 (pending_promotion) 的技能 → 向 Lead 发起转正审批.

        复用 plan_approval 通道 (PLAN_APPROVAL_REQUEST 消息), 内容含技能
        全文 + 使用统计 + 程序判定的风险标记; 审批结果在
        _handle_inbox_message 的 PLAN_APPROVAL_RESPONSE 路由里 promote/archive。
        """
        try:
            store = self._skill_evolution_store
            if store is None or not self._lead_name:
                return
            from harness.skills.evolution.member import (
                send_promotion_approval_request,
            )
            from harness.team.models import RequestStatus

            for record in store.pending_promotions(self.name):
                content = store.read_skill_content(self.name, record["name"])
                req_id = await send_promotion_approval_request(
                    self._message_bus,
                    from_agent=self.name,
                    lead_name=self._lead_name,
                    record=record,
                    skill_content=content,
                )
                if req_id is None:
                    continue
                # ── 登记协议追踪 (type=skill_promotion, 响应路由据此转正/归档) ──
                async with self._tracker_lock:
                    self._pending_requests[req_id] = {
                        "type": "skill_promotion",
                        "status": RequestStatus.PENDING,
                        "from": self.name,
                        "skill": record["name"],
                    }
                store.mark_promotion_requested(self.name, record["name"])
                logger.info(
                    "Skill promotion approval requested: agent=%s skill=%s req=%s",
                    self.name, record["name"], req_id,
                )
        except Exception as exc:
            logger.warning(
                "Skill promotion request failed for '%s': %s", self.name, exc,
            )

    async def _write_member_lessons(self, task_id: str) -> None:
        """任务终态后程序式提取成员经验写 L3 (fire-and-forget, 不跑 LLM).

        提取规则见 member_memory.extract_lessons_from_task — 与 orchestrator
        结算时的分发共用, source_task_id 幂等去重保证两处写入不重复。
        失败静默: best-effort, 不阻塞 agent loop。
        """
        try:
            if not self._project_id:
                return
            task = await self._task_store.get_task(task_id)
            if task is None:
                return
            for kind, text in extract_lessons_from_task(task):
                await self._member_memory_store.add_lesson(
                    self._project_id, self.name, kind, text,
                    source_task_id=task.id,
                )
        except Exception as exc:
            logger.warning(
                "Member lesson write failed for '%s': %s", task_id, exc,
            )

    async def _extract_task_memory(self, task_id: str) -> None:
        """Extract structured memory from a completed task (fire-and-forget).

        Called after task settlement when the task completed successfully.
        Failure is silent — extraction is best-effort and should never
        block or crash the agent loop.
        """
        try:
            task = await self._task_store.get_task(task_id)
            if task is None:
                return
            effective_output = task.effective_output()
            if not effective_output or not effective_output.strip():
                logger.debug("Task '%s' has no output, skipping memory extraction", task_id)
                return

            from harness.memory.prompt import TASK_MEMORY_UPDATE_PROMPT
            from harness.memory.updater import _create_memory_model, _extract_text

            # ── build extraction prompt ──
            prompt = TASK_MEMORY_UPDATE_PROMPT.format(
                task_title=task.title,
                task_description=task.description or "(no description)",
                task_output=effective_output[:3000],
                task_status=task.status.value,
                assigned_agent=self.name,
            )

            # ── create lightweight LLM ──
            api_key = self._extract_api_key()
            base_url = self._extract_base_url()
            model_name = (
                self._effective_config.memory_model
                or self._effective_config.model
                or "gpt-4o-mini"
            ) if self._effective_config else "gpt-4o-mini"

            model = _create_memory_model(model_name, api_key=api_key, base_url=base_url)
            if model is None:
                return

            response = await model.ainvoke(prompt)
            text = _extract_text(response.content).strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
            data = json.loads(text)

            from harness.memory.task_memory import TaskMemory

            memory = TaskMemory(
                task_id=task.id,
                task_title=task.title,
                assigned_agent=self.name,
                status=task.status.value,
                summary=data.get("summary", ""),
                decisions=data.get("decisions", []),
                pitfalls=data.get("pitfalls", []),
                discoveries=data.get("discoveries", []),
                tags=data.get("tags", []),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            await self._task_memory_store.save(memory)
            logger.info(
                "Task memory extracted for '%s': %d decisions, %d pitfalls, "
                "%d discoveries, tags=%s",
                task_id, len(memory.decisions), len(memory.pitfalls),
                len(memory.discoveries), memory.tags,
            )
        except json.JSONDecodeError:
            logger.warning("Failed to parse task memory LLM response for '%s'", task_id)
        except Exception as exc:
            logger.warning("Task memory extraction failed for '%s': %s", task_id, exc)

    def _extract_api_key(self) -> str:
        """Get API key for memory extraction LLM."""
        if self._effective_config and self._effective_config.api_key:
            return self._effective_config.api_key
        return os.environ.get("OPENAI_API_KEY", "")

    def _extract_base_url(self) -> str:
        """Get base URL for memory extraction LLM."""
        if self._effective_config and self._effective_config.base_url:
            return self._effective_config.base_url
        return os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

    # ------------------------------------------------------------------
    # 外部唤醒
    # ------------------------------------------------------------------

    async def assign_task(self, task: TeamTask) -> bool:
        """外部唤醒: 分配任务给此 teammate. 返回是否受理.

        非 IDLE 时拒绝并返回 False — 调用方 (Orchestrator) 必须据此回滚任务状态,
        防止任务卡在 IN_PROGRESS 无人执行.
        检查 IDLE 与置 WORKING 之间无 await, 单事件循环下是原子操作, 无 TOCTOU 窗口.
        """
        if self.status != TeammateStatus.IDLE:
            logger.warning(
                "Teammate '%s' is not idle (status=%s), cannot assign task",
                self.name, self.status,
            )
            return False

        # ── 构建任务消息 ──
        content_parts = [
            f"<assigned_task>\n"
            f"  <task_id>{task.id}</task_id>\n"
            f"  <title>{task.title}</title>\n"
            f"  <description>{task.description}</description>\n"
            f"  <priority>{task.priority}</priority>\n"
            f"</assigned_task>",
        ]

        # ── 结构化 spec 注入 (创建工具已渲染进 description 时不重复注入) ──
        if task.spec is not None and not task.spec.is_empty():
            spec_text = task.spec.render()
            if spec_text and spec_text not in (task.description or ""):
                content_parts.append(f"\n<task_spec>\n{spec_text}\n</task_spec>")

        # ── 注入依赖任务的执行结果 ──
        if task.dependencies:
            dep_results: list[str] = []
            for dep_id in task.dependencies:
                dep_task = await self._task_store.get_task(dep_id)
                if dep_task is None:
                    continue
                if not dep_task.status.is_success:
                    continue
                dep_text = (
                    f"\n<dependency_result>\n"
                    f"  <task_id>{dep_task.id}</task_id>\n"
                    f"  <title>{dep_task.title}</title>\n"
                    f"  <executor>{dep_task.assigned_agent or 'unknown'}</executor>\n"
                    f"  <output>{dep_task.effective_output() or '(no output)'}</output>\n"
                    f"</dependency_result>"
                )
                dep_results.append(dep_text)
            if dep_results:
                content_parts.append(
                    "\n<dependency_results>\n"
                    f"Below are the results of the prerequisite tasks you depend on. Complete your task based on these results:"
                    + "".join(dep_results)
                    + "\n</dependency_results>"
                )

        # ── 注入相关历史任务记忆 (压缩格式, 每条 ~80 tokens) ──
        if (self._task_memory_store is not None
                and task.title and task.description):
            try:
                from harness.memory.prompt import format_related_tasks_for_injection
                related = await self._task_memory_store.find_related(
                    task.title, task.description, max_results=3,
                )
                if related:
                    memory_block = format_related_tasks_for_injection(related)
                    if memory_block:
                        content_parts.append(f"\n{memory_block}")
            except Exception as exc:
                logger.debug(
                    "Failed to inject task memory for '%s': %s",
                    task.id, exc,
                )

        # REVISION_NEEDED: 注入审查反馈 (Lead task_review 或 Verifier 验收结论)
        if task.status == TeamTaskStatus.REVISION_NEEDED and task.review_feedback:
            content_parts.append(
                f"\n<review_feedback>\n"
                f"⚠️ Review/verification feedback (revision #{task.revision_count}):\n"
                f"{task.review_feedback}\n"
                f"Revise your implementation according to the feedback above, then resubmit for review.\n"
                f"</review_feedback>"
            )

        # 完成指引
        content_parts.append(
            f"\nPlease complete the task above. When done, use task_update to set the status to in_review "
            f"(recommended; low-risk tasks pass evidence checks directly, high-risk tasks are verified by the Verifier/Lead) "
            f"or completed (finish directly), and attach a result JSON: "
            f'{{"output": "outcome summary", "evidence": ["evidence"], "uncertainty": "low|medium|high"}} '
            f"(lightweight tasks may fill in output only).\n"
            f"If it fails, use task_update to set the status to failed and explain the reason in result.failure_reason."
        )

        self.current_task_id = task.id
        self._current_task_is_synthesis = (task.title or "").startswith("汇总:")
        # ── 缓存任务文本, 供 _build_system_prompt 检索 L3 成员经验 ──
        self._current_task_text = " ".join(filter(None, [
            task.title or "",
            task.description or "",
            task.spec.goal if task.spec else "",
        ]))
        self._messages.append(HumanMessage(content="\n".join(content_parts)))
        self.status = TeammateStatus.WORKING
        self._wake_event.set()
        # ── 写入 agent 日志: 任务分配 (human 消息) ──
        if self._agent_log_writer:
            self._agent_log_writer.write_message(
                role="human",
                content=f"任务: {task.title}\n\n{task.description or '(无描述)'}",
                task_id=task.id,
            )
        logger.info("Teammate '%s' assigned task '%s' (rev=%d)", self.name, task.id, task.revision_count)
        return True

    # ------------------------------------------------------------------
    # 消息处理
    # ------------------------------------------------------------------

    def _stage_message(self, content: str) -> None:
        """注入消息到对话历史; 若当前空闲则置 WORKING, 等待唤醒后消费."""
        self._messages.append(HumanMessage(content=content))
        if self.status == TeammateStatus.IDLE:
            self.status = TeammateStatus.WORKING

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
            self._stage_message(
                f"<shutdown_request>\n"
                f"  <request_id>{req_id}</request_id>\n"
                f"  <from>{msg.from_agent}</from>\n"
                f"  <message>{msg.content}</message>\n"
                f"</shutdown_request>\n\n"
                f"You received a shutdown request from '{msg.from_agent}'. Evaluate your current state, then reply with the shutdown_response tool:\n"
                f"- If no critical task is in progress, approve the shutdown:\n"
                f"  shutdown_response(request_id=\"{req_id}\", requester=\"{msg.from_agent}\", approve=True)\n"
                f"- If a critical task is in progress (e.g. writing files), reject the shutdown:\n"
                f"  shutdown_response(request_id=\"{req_id}\", requester=\"{msg.from_agent}\", approve=False, reason=\"Executing a critical task...\")\n\n"
                f"Note: after approve=True the agent will exit gracefully at the end of the current turn (after finishing the current tool call)."
            )
            logger.info("Teammate '%s' received shutdown_request (%s), injected into messages for LLM decision",
                        self.name, req_id)
            return

        # ── shutdown_response — Lead 收到 teammate 的确认 ──
        if msg.msg_type == TeamMessageType.SHUTDOWN_RESPONSE:
            async with self._tracker_lock:
                if msg.request_id and msg.request_id in self._pending_requests:
                    self._pending_requests[msg.request_id]["status"] = _resolve_approval_status(msg)
            # 注入消息并唤醒 (对齐 PLAN_APPROVAL_RESPONSE 处理),
            # 否则 Lead 只更新 _pending_requests, 永远等不到关机确认
            self._stage_message(
                f"<shutdown_response>\n"
                f"  <request_id>{msg.request_id}</request_id>\n"
                f"  <from>{msg.from_agent}</from>\n"
                f"  <result>{msg.content}</result>\n"
                f"</shutdown_response>\n\n"
                f"Shutdown confirmation from '{msg.from_agent}'. Record the result and continue orchestration."
            )
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
            self._stage_message(
                f"<plan_approval_request>\n"
                f"  <request_id>{req_id}</request_id>\n"
                f"  <from>{msg.from_agent}</from>\n"
                f"  <plan>{msg.content}</plan>\n"
                f"</plan_approval_request>\n\n"
                f"Plan approval request from '{msg.from_agent}'. Review it, then reply with the approve_plan tool:\n"
                f"- Approve: approve_plan(request_id=\"{req_id}\", requester=\"{msg.from_agent}\", approve=True, feedback=\"additional suggestions...\")\n"
                f"- Reject: approve_plan(request_id=\"{req_id}\", requester=\"{msg.from_agent}\", approve=False, feedback=\"rejection reason...\")\n\n"
                f"Approval criteria: Is the plan safe? Is it aligned with project goals? Is there a better approach?"
            )
            return

        # ── plan_approval_response — Teammate 收到 Lead 的审批结果 ──
        if msg.msg_type == TeamMessageType.PLAN_APPROVAL_RESPONSE:
            async with self._tracker_lock:
                req = self._pending_requests.get(msg.request_id) if msg.request_id else None
                if msg.request_id and msg.request_id in self._pending_requests:
                    self._pending_requests[msg.request_id]["status"] = _resolve_approval_status(msg)
            # ── Phase 5: 技能转正审批结果 → promote (批准) / archive (拒绝) ──
            if (req is not None and req.get("type") == "skill_promotion"
                    and self._skill_evolution_store is not None):
                try:
                    if _resolve_approval_status(msg) == RequestStatus.APPROVED:
                        self._skill_evolution_store.promote(self.name, req["skill"])
                    else:
                        self._skill_evolution_store.archive(self.name, req["skill"])
                except Exception:
                    logger.warning(
                        "Failed to apply skill promotion result for '%s' skill '%s'",
                        self.name, req.get("skill"), exc_info=True,
                    )
            self._stage_message(
                f"<plan_approval_response>\n"
                f"  <request_id>{msg.request_id}</request_id>\n"
                f"  <from>{msg.from_agent}</from>\n"
                f"  <result>{msg.content}</result>\n"
                f"</plan_approval_response>"
            )
            return

        # lifecycle 通知 — 唤醒 Lead 让其监控团队进度
        if msg.msg_type == TeamMessageType.LIFECYCLE:
            self._stage_message(
                f"[Team notification] {msg.from_agent} {msg.content}\n\n"
                f"As Lead, check the team status: use list_teammates to view member status, "
                f"use task_list to view task progress. If needed, create new tasks or reassign."
            )
            return

        # 普通消息 / 广播
        self._stage_message(f"[From {msg.from_agent}] {msg.content}")

    def _inject_identity(self) -> None:
        """注入身份块 — 防止长上下文后遗忘自己是谁.

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
    # Agent 日志实时写入 (Member 专用, 避免污染主 SSE 流)
    # ------------------------------------------------------------------

    def _flush_ai_buffer(self) -> None:
        """将积累的 AI 流式消息 buffer 写入 JSONL 并清空."""
        if self._streaming_ai_buffer.strip() and self._agent_log_writer and self._streaming_ai_task_id:
            self._agent_log_writer.write_message(
                role="ai",
                content=self._streaming_ai_buffer.strip(),
                task_id=self._streaming_ai_task_id,
            )
        self._streaming_ai_buffer = ""
        self._streaming_ai_task_id = None

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
        role = self._role or "member"
        return TeamMemberRuntime(
            agent_name=self.name,
            role=role,
            status=self.status,
            current_task_id=self.current_task_id,
            completed_tasks=self.completed_tasks,
            failed_tasks=self.failed_tasks,
            last_error=self.last_error,
        )
