"""Team 工具集 — 仅在 mode=team 时注册, 按角色分层.

15 个工具, 分三层:
  Lead 专属 (7):  delegate_to_member, list_teammates, broadcast, shutdown_teammate,
                   approve_plan, spawn_teammate, task_review
  共享 (5):        task_create, task_list, send_message, read_inbox, memory_search
  Member 专属 (3): task_update, request_plan_approval, shutdown_response

工具中 agent 身份通过 ContextVar 自动注入.
"""

from __future__ import annotations

import json
import logging
import uuid as _uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from harness.config.paths import get_paths
from harness.team.message_bus import _PROTOCOL_MESSAGE_TYPES
from harness.team.models import TaskResult, TaskSpec, TeamTaskStatus, infer_task_risk

logger = logging.getLogger(__name__)

# ── 当前 Agent 上下文 ──
_current_agent: ContextVar[str] = ContextVar("current_agent", default="unknown")
_current_agent_instance: ContextVar[Any] = ContextVar("current_agent_instance", default=None)


def set_current_agent(name: str) -> None:
    _current_agent.set(name)


def get_current_agent() -> str:
    return _current_agent.get()


def set_current_agent_instance(instance: Any) -> None:
    """注入当前 TeammateAgent 实例引用 (供工具访问 agent 内部状态)."""
    _current_agent_instance.set(instance)


def get_current_agent_instance() -> Any:
    """获取当前 TeammateAgent 实例 (可能为 None)."""
    return _current_agent_instance.get()


def _format_memory_detail(memory) -> str:
    """Format a single task memory for full-detail display.

    Used by the ``memory_search`` tool when returning results.
    """
    lines = [
        f"## [{memory.task_id}] {memory.task_title}",
        f"Executor: {memory.assigned_agent or 'unknown'} | Status: {memory.status}",
    ]
    if memory.summary:
        lines.append(f"Summary: {memory.summary}")
    if memory.decisions:
        lines.append("Decisions:")
        lines.extend(f"  - {d}" for d in memory.decisions)
    if memory.pitfalls:
        lines.append("Pitfalls:")
        lines.extend(f"  - {p}" for p in memory.pitfalls)
    if memory.discoveries:
        lines.append("Discoveries:")
        lines.extend(f"  - {d}" for d in memory.discoveries)
    if memory.tags:
        lines.append(f"Tags: {', '.join(memory.tags)}")
    return "\n".join(lines)


# ── 状态流转校验 ──
# 注意: INTERRUPTED 有意不在表中 — interrupted 任务只能由 orchestrator 的
# 恢复流程 (recover_orphaned_tasks / _resume_interrupted_tasks) 处理,
# member 不可通过 task_update 手动流转。
_VALID_TRANSITIONS: dict[TeamTaskStatus, set[TeamTaskStatus]] = {
    TeamTaskStatus.PENDING: {TeamTaskStatus.IN_PROGRESS},
    TeamTaskStatus.IN_PROGRESS: {TeamTaskStatus.IN_REVIEW, TeamTaskStatus.COMPLETED, TeamTaskStatus.FAILED},
    TeamTaskStatus.IN_REVIEW: {TeamTaskStatus.APPROVED, TeamTaskStatus.REVISION_NEEDED},
    TeamTaskStatus.REVISION_NEEDED: {TeamTaskStatus.IN_PROGRESS},
}
# 终态不可再变更
for _terminal in (TeamTaskStatus.APPROVED, TeamTaskStatus.COMPLETED, TeamTaskStatus.FAILED, TeamTaskStatus.CANCELLED):
    _VALID_TRANSITIONS[_terminal] = set()


def _allowed_transitions(current: TeamTaskStatus) -> set[TeamTaskStatus]:
    """Return the set of statuses that *current* can legally transition to."""
    return _VALID_TRANSITIONS.get(current, set())


# ── 角色工具集定义 ──
LEAD_TOOLS = {"delegate_to_member", "list_teammates", "broadcast", "shutdown_teammate", "approve_plan", "spawn_teammate", "task_review"}
SHARED_TOOLS = {"task_create", "task_list", "send_message", "read_inbox", "memory_search"}
MEMBER_TOOLS = {"task_update", "request_plan_approval", "shutdown_response"}


# ── 任务协议 JSON 化辅助 (Phase 2) ──

def _normalize_str_list(value: Any) -> list[str]:
    """宽松归一化 list 参数: LLM 可能传 list、按行分隔的字符串或 JSON 编码字符串
    (如 "[\\"a\\", \\"b\\"]" — E2E 观察到的真实行为), 一律降级容忍不报错."""
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(v) for v in parsed if str(v).strip()]
            except (json.JSONDecodeError, ValueError):
                pass  # 非 JSON → 按行分割兜底
        return [ln.strip() for ln in value.splitlines() if ln.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


def _build_spec(
    goal: str = "", background: str = "", description: str = "",
    constraints: Any = None, format: str = "", acceptance_criteria: Any = None,
) -> TaskSpec | None:
    """由各工具参数组装 TaskSpec; 无结构化字段时返回 None (纯文本降级路径).

    description 单独出现不算结构化 — 它是纯文本降级路径的载体,
    只有 goal/background/constraints/format/acceptance_criteria 任一非空才生成 spec。
    """
    spec = TaskSpec(
        background=background or "",
        goal=goal or "",
        description=description or "",
        constraints=_normalize_str_list(constraints),
        format=format or "",
        acceptance_criteria=_normalize_str_list(acceptance_criteria),
    )
    if (not spec.goal and not spec.background and not spec.constraints
            and not spec.format and not spec.acceptance_criteria):
        return None
    return spec


def _submission_requirement(task_id: str) -> str:
    """提交要求模板 — 引导 member 输出 result JSON."""
    return (
        f"\n\n[Submission Requirement]\n"
        f"When done, submit for review with task_update: "
        f"task_update(task_id=\"{task_id}\", status=\"in_review\", result={{...}}).\n"
        f"result is a JSON object with fields:\n"
        f'- output: outcome summary (required)\n'
        f'- evidence: evidence list (file paths/commands/links, optional)\n'
        f'- uncertainty: self-assessed uncertainty "low"|"medium"|"high" (default low, informational only)\n'
        f'- failure_reason: failure reason (required when status="failed")\n'
        f"Light tasks may fill only output; on failure use status=\"failed\" and fill failure_reason.\n"
        f"[Acceptance Path] Low-risk tasks: completed directly once evidence validation passes (no review); "
        f"high-risk tasks: reviewed by an independent Verifier or the Lead; failure sends the task back for rework."
    )


def _validate_evidence(
    evidence: list[str], workspace_roots: list[Path],
) -> tuple[bool, list[str]]:
    """程序校验证据中的文件路径存在性 (Phase 3 低危直通).

    链接 (含 ://) 跳过不查; 相对路径在各工作区根下解析, 绝对路径直接检查;
    无法解析根目录时视为缺失 (fail-safe, 调用方转 Lead 审查)。
    返回 (是否全部通过, 缺失条目列表)。
    """
    missing: list[str] = []
    for entry in evidence:
        e = (entry or "").strip()
        if not e or "://" in e:
            continue  # 空串/链接不做存在性检查
        p = Path(e)
        if p.is_absolute():
            candidates = [p]
            # 沙箱绝对路径 (/mnt/user-data/...) → 映射回宿主机 thread 工作区,
            # 否则宿主机上必然不存在, 永远 fail-safe 转人工审查 (E2E 观察到)
            if e.startswith("/mnt/user-data/"):
                rel = e[len("/mnt/user-data/"):]
                candidates += [root / rel for root in workspace_roots]
        else:
            candidates = [root / e for root in workspace_roots]
        if not any(c.exists() for c in candidates):
            missing.append(e)
    return (not missing, missing)


def _evidence_workspace_roots(agent_name: str) -> list[Path]:
    """证据文件解析的工作区根目录 (成员私有 workspace + 线程共享 workspace + 线程根).

    无法确定线程上下文 (无 agent 实例/thread_id) 时返回空列表 —
    调用方按 fail-safe 处理 (证据无法校验 → 转 Lead 审查)。
    """
    roots: list[Path] = []
    try:
        instance = get_current_agent_instance()
        thread_id = getattr(instance, "_thread_id", "") if instance is not None else ""
        user_id = getattr(instance, "_user_id", "default") if instance is not None else "default"
        if not thread_id:
            return roots
        paths = get_paths()
        tdir = paths.thread_dir(thread_id, user_id=user_id)
        roots.append(tdir / "agents" / agent_name / "workspace")
        roots.append(paths.sandbox_work_dir(thread_id, user_id=user_id))
        roots.append(tdir)
        # /mnt/user-data/ 沙箱前缀映射的宿主机落点 (证据绝对路径解析用)
        roots.append(tdir / "user-data")
    except Exception:
        pass
    return roots


def _parse_task_result(raw: Any, status: str = "") -> TaskResult | None:
    """解析 member 提交的 result JSON; 解析失败返回 None (调用方降级为纯文本)."""
    data: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        # 容忍 ```json 代码块包裹
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(data, dict):
        return None
    try:
        result = TaskResult(**data)
    except Exception:
        return None
    if not result.status and status:
        result.status = status
    return result


def create_team_tools(
    task_store: Any = None,
    message_bus: Any = None,
    teammates: dict | None = None,
    role: str = "member",
    spawn_callback: Any = None,   # async callable(agent_name: str) -> str
    event_emitter: Any = None,    # async callable(event: dict) — SSE 事件发射器
    lead_name: str | None = None, # Lead 名称 — Member 的协议消息定向发送给 Lead
    progress_callback: Any = None,# callable() — 唤醒 dispatch 循环
    member_names: list[str] | None = None,  # 名册 (懒加载: 含未 spawn 成员)
    run_started_at: Any = None,   # callable() -> str — 本次 run 开始时间 (历史任务打标)
) -> list[BaseTool]:
    """构建 Team 模式专用工具集, 按角色过滤.

    Args:
        role: "lead" | "member" — 决定返回哪些工具.
              lead: LEAD_TOOLS + SHARED_TOOLS (12 个)
              member: SHARED_TOOLS + MEMBER_TOOLS (8 个)
        spawn_callback: Lead 专属, 用于动态 spawn 新 teammate 的回调.
        event_emitter: SSE 事件发射器.
        lead_name: Lead Agent 名称.
        progress_callback: 唤醒 Orchestrator dispatch 循环 (task_create 后调用).
    """

    # ── 通用辅助 (闭包) ──

    async def _emit_task_update(task: Any) -> None:
        """SSE: 推送任务状态变更到前端 (best-effort)."""
        if event_emitter is None:
            return
        try:
            await event_emitter({
                "type": "team_task_update",
                "task": task.model_dump(),
            })
        except Exception:
            pass

    def _wake() -> None:
        """唤醒 dispatch 循环 (best-effort)."""
        if progress_callback is None:
            return
        try:
            progress_callback()
        except Exception:
            pass

    async def _check_dependencies(dep_list: list[str]) -> list[str]:
        """依赖校验, 返回警告列表 (不阻塞创建)."""
        dep_warnings: list[str] = []
        if not dep_list or task_store is None:
            return dep_warnings
        all_tasks = await task_store.load_tasks()
        task_map = {t.id: t for t in all_tasks}
        for dep_id in dep_list:
            dep_task = task_map.get(dep_id)
            if dep_task is None:
                dep_warnings.append(f"⚠️ Dependency '{dep_id}' does not exist")
            elif dep_task.status in (TeamTaskStatus.FAILED, TeamTaskStatus.CANCELLED):
                dep_warnings.append(
                    f"⚠️ Dependency '{dep_id}' ({dep_task.title}) is already in terminal state "
                    f"'{dep_task.status.value}'; the current task will be blocked forever"
                )
        return dep_warnings

    async def _apply_task_risk(task: Any, risk_param: str = "") -> str:
        """落定任务风险等级 (Phase 3): Lead 标 high → 锁定; Lead 标 low 但程序
        推断 high → 单向升级为 high (fail-safe: LLM 自评低风险不可信,
        E2E 观察到 Lead 给写文件任务标 low); 未指定 → 程序推断."""
        explicit = (risk_param or "").strip().lower()
        if explicit == "high":
            await task_store.update_task(task.id, risk="high", risk_locked=True)
            return "high"
        inferred = infer_task_risk(task)
        if explicit == "low":
            if inferred == "low":
                await task_store.update_task(task.id, risk="low", risk_locked=True)
                return "low"
            await task_store.update_task(task.id, risk="high")
            return "high (program review: Lead marked low but write-operation signals matched; upgraded)"
        await task_store.update_task(task.id, risk=inferred)
        return inferred

    async def _upgrade_dependency_risk(dep_list: list[str]) -> None:
        """被依赖的任务自动升级为 high (有下游依赖它的产出, 需强制验收).

        仅升级未被 Lead 显式锁定 (risk_locked=False) 的任务。
        """
        if task_store is None:
            return
        for dep_id in dep_list or []:
            dep = await task_store.get_task(dep_id)
            if dep is not None and not dep.risk_locked and dep.risk != "high":
                await task_store.update_task(dep_id, risk="high")

    # ═════════════════════════════════════════════════════════════════
    # Lead 专属工具
    # ═════════════════════════════════════════════════════════════════

    @tool
    async def delegate_to_member(
        agent_name: str,
        title: str,
        goal: str = "",
        background: str = "",
        description: str = "",
        constraints: Any = None,
        format: str = "",
        acceptance_criteria: Any = None,
        dependencies: Any = None,
        priority: str = "medium",
        risk: str = "",
    ) -> str:
        """Create a task and delegate it to the given Team Member Agent (Lead only, one step).

        The task is assigned to the target member immediately after creation; the orchestrator's dispatch loop picks it up automatically.
        The task must be self-contained: members cannot see your conversation history, so write the background/goal/constraints in full.
        Providing only a plain-text description also works (light-task fallback path), but complex tasks should use the structured fields.

        Acceptance path by risk level (Phase 3):
        - Low risk (read-only/exploration/query): after the member submits, evidence is validated programmatically; pass → completed directly (no review)
        - High risk (write operations/acceptance criteria/downstream dependents): mandatory acceptance — when the team has a Verifier member,
          an independent acceptance sub-task is created automatically; without a Verifier, you review it with task_review.
        Use the risk parameter to set the level explicitly; otherwise the system infers it by rules.

        Args:
            agent_name: target Member Agent name
            title: task title
            goal: goal (what to deliver)
            background: background (why, necessary verbatim context)
            description: detailed description (used as the plain-text task description when structured fields are empty)
            constraints: list of constraints/caveats (tech stack/boundaries/forbidden items)
            format: output format requirements
            acceptance_criteria: list of acceptance criteria
            dependencies: list of task IDs this task depends on (dispatched only after dependencies complete)
            priority: "low"|"medium"|"high"|"critical"
            risk: risk level "low"|"high" (empty = system-inferred: write operations/acceptance criteria/downstream dependents → high)
        """
        if task_store is None:
            return "Error: Task store not available"

        # ── 成员存在性检查 (懒加载: 名册 + 已 spawn 都算在团队中) ──
        member_warning = ""
        known = set(teammates.keys() if teammates else ()) | set(member_names or ())
        if known and agent_name not in known:
            member_warning = (
                f"\n⚠️ Warning: member '{agent_name}' is not in the current team."
                f"Available members: {', '.join(sorted(known))}"
                f"\nThe task will still be created, but needs manual reassignment or waiting for the member to join."
            )

        # ── 组装结构化 spec (缺字段/纯文本时降级: spec=None, 只用 description) ──
        spec = _build_spec(
            goal=goal, background=background, description=description,
            constraints=constraints, format=format,
            acceptance_criteria=acceptance_criteria,
        )
        # 任务描述: 有 spec 用渲染文本, 无 spec 用纯文本 (降级路径)
        full_desc = spec.render() if spec is not None else (description or goal or "")

        dep_list = _normalize_str_list(dependencies)
        dep_warnings = await _check_dependencies(dep_list)

        task = await task_store.create_task(
            title=title,
            description=full_desc,
            assigned_agent=agent_name,
            dependencies=dep_list,
            priority=priority,
            spec=spec,
        )
        # ── Phase 3: 落定风险等级 + 被依赖任务升级 high ──
        final_risk = await _apply_task_risk(task, risk)
        await _upgrade_dependency_risk(dep_list)
        # 提交要求含 task_id, 需创建后追加
        await task_store.update_task(
            task.id,
            description=full_desc + _submission_requirement(task.id),
        )
        task = await task_store.get_task(task.id) or task
        # ── SSE: 推送任务创建事件到前端 ──
        await _emit_task_update(task)
        # ── 唤醒 dispatch 循环 ──
        _wake()

        result = (
            f"Created and delegated task [{task.id}] to '{agent_name}'.\n"
            f"Title: {title}\n"
            f"Priority: {priority}\n"
            f"Risk level: {final_risk} ({'low risk: completed directly once evidence validation passes' if final_risk == 'low' else 'high risk: mandatory independent acceptance'})"
        )
        if spec is not None and spec.goal:
            result += f"\nGoal: {spec.goal[:200]}"
        if dep_list:
            result += f"\nDependencies: {', '.join(dep_list)}"
        if dep_warnings:
            result += "\n\n" + "\n".join(dep_warnings)
        return result + member_warning

    @tool
    async def list_teammates() -> str:
        """Show the current status of all Members in the Team (Lead only).

        Lists Member Agents only, not the Lead itself.
        """
        if teammates is None:
            return "Teammate list unavailable."
        members = {
            name: tm for name, tm in teammates.items()
            if getattr(tm, "_role", "") != "lead"
        }
        # ── 懒加载: 名册中未 spawn 的成员也列出 (语义上可用, 派单时自动拉起) ──
        standby = [n for n in (member_names or []) if n not in members]
        if not members and not standby:
            return "The current Team has no Members (Lead only)."
        lines = [f"Total {len(members) + len(standby)} Members:\n"]
        for name, tm in members.items():
            icon = {"idle": "🟢", "working": "🔵", "failed": "❌"}.get(
                tm.status.value if hasattr(tm.status, 'value') else str(tm.status), "❓")
            task_info = f" (task: {tm.current_task_id})" if tm.current_task_id else ""
            lines.append(f"- {icon} **{name}** [{tm.status}] — completed {tm.completed_tasks}{task_info}")
        for name in standby:
            lines.append(f"- ⚪ **{name}** [standby] — pending spawn (auto-started on dispatch)")
        return "\n".join(lines)

    @tool
    async def shutdown_teammate(agent_name: str) -> str:
        """ Request to shut down the given teammate (Lead only)."""
        if message_bus is None:
            return "Error: Message bus not available"
        from harness.team.models import RequestStatus, TeamMessage, TeamMessageType
        req_id = str(_uuid.uuid4())[:8]
        msg = TeamMessage(
            from_agent=get_current_agent(), to_agent=agent_name,
            msg_type=TeamMessageType.SHUTDOWN_REQUEST, content="shutdown", request_id=req_id,
        )
        await message_bus.send(msg)
        # ── 登记协议追踪 (发起方) — 否则响应回来时 _pending_requests 永不命中 ──
        agent = get_current_agent_instance()
        if agent is not None:
            async with agent._tracker_lock:
                agent._pending_requests[req_id] = {
                    "type": "shutdown",
                    "status": RequestStatus.PENDING,
                    "target": agent_name,
                }
        return f"Shutdown request sent to '{agent_name}' (req_id={req_id})"

    @tool
    async def approve_plan(request_id: str, requester: str, approve: bool, feedback: str = "") -> str:
        """Approve a plan submitted by a Teammate — structured approval (Lead only).

        After receiving a plan_approval_request, review the plan content and reply with this tool:
        - approve=True: approve the plan; the Teammate will continue execution
        - approve=False: reject the plan; the Teammate must adjust and resubmit

        Args:
            request_id: ID of the plan approval request (must match the received request)
            requester: name of the Agent that submitted the plan
            approve: whether to approve the plan
            feedback: approval feedback (optional suggestions when approving; required reason when rejecting)
        """
        if message_bus is None:
            return "Error: Message bus not available"
        from harness.team.models import TeamMessage, TeamMessageType

        status_text = f"approved. {feedback}" if approve else f"rejected: {feedback or 'plan not approved'}"
        msg = TeamMessage(
            from_agent=get_current_agent(), to_agent=requester,
            msg_type=TeamMessageType.PLAN_APPROVAL_RESPONSE,
            content=status_text, request_id=request_id,
            approved=approve,  # 结构化结果 — 接收方优先读此字段
        )
        await message_bus.send(msg)

        # 更新本地追踪器 (加锁, 与 _handle_inbox_message 的加锁写对齐)
        agent = get_current_agent_instance()
        if agent is not None:
            async with agent._tracker_lock:
                agent._pending_requests[request_id] = {
                    "type": "plan_approval",
                    "status": "approved" if approve else "rejected",
                    "from": requester,
                    "feedback": feedback,
                }

        action = "Approved" if approve else "Rejected"
        return f"{action} the plan from '{requester}' (req_id={request_id})."

    @tool
    async def spawn_teammate(agent_name: str) -> str:
        """Dynamically create and start a new Teammate Agent (Lead only).

        Call this tool when you need to expand the team. The new teammate will:
        1. Use its preconfigured SOUL.md as system prompt
        2. Automatically enter IDLE state, waiting for task assignment
        3. Support  self-claiming unassigned tasks on the task board

        Args:
            agent_name: name of the Agent to create (must exist in the agents config)
        """
        if spawn_callback is None:
            return "Error: Spawn not available (no orchestrator callback)"
        try:
            result = await spawn_callback(agent_name)
            return result
        except Exception as exc:
            return f"Error: Failed to spawn '{agent_name}': {exc}"

    @tool
    async def task_review(task_id: str, approve: bool, feedback: str = "") -> str:
        """Review a task submitted by a member (Lead only).

        After a member finishes a task and submits it for review via task_update(status="in_review"),
        you should review its output and decide to approve or request changes.

        Args:
            task_id: ID of the task to review
            approve: True = approve; the task becomes approved (terminal)
            feedback: review comments. Optional brief remarks when approving; when requesting changes,
                      you must specify exactly what to change so the member knows what to fix.
        """
        if task_store is None:
            return "Error: Task store not available"

        task = await task_store.get_task(task_id)
        if task is None:
            return f"Error: Task '{task_id}' not found"
        if task.status != TeamTaskStatus.IN_REVIEW:
            return (
                f"Error: Task '{task_id}' is currently '{task.status.value}', "
                f"not 'in_review', so it cannot be reviewed. Only tasks submitted by a member "
                f"via task_update(status=\"in_review\") can be reviewed."
            )

        if approve:
            await task_store.update_task(
                task_id,
                status=TeamTaskStatus.APPROVED,
                review_feedback=feedback or "Approved",
            )
            # ── SSE: 推送任务状态变更 ──
            updated = await task_store.get_task(task_id)
            if updated:
                await _emit_task_update(updated)
            # ── 唤醒 dispatch 循环 (依赖此任务的下游任务可能已解锁) ──
            _wake()
            # ── 通知成员审批通过 ──
            if message_bus is not None and task.assigned_agent:
                from harness.team.models import TeamMessage, TeamMessageType
                msg = TeamMessage(
                    from_agent=get_current_agent(),
                    to_agent=task.assigned_agent,
                    msg_type=TeamMessageType.TEXT,
                    content=(
                        f"Task [{task_id}] '{task.title}' approved ✅"
                        + (f" — {feedback}" if feedback else "")
                    ),
                    task_id=task_id,
                )
                await message_bus.send(msg)
            return (
                f"Approved task [{task_id}] '{task.title}'."
                + (f" Comments: {feedback}" if feedback else "")
            )

        # ── 要求修改 ──
        if not feedback:
            return "Error: feedback is required when requesting changes — specify exactly what needs to be changed."
        new_revision = task.revision_count + 1
        await task_store.update_task(
            task_id,
            status=TeamTaskStatus.REVISION_NEEDED,
            review_feedback=feedback,
            revision_count=new_revision,
        )
        # ── SSE: 推送任务状态变更 ──
        updated = await task_store.get_task(task_id)
        if updated:
            await _emit_task_update(updated)
        # ── 发送消息通知成员 ──
        if message_bus is not None and task.assigned_agent:
            from harness.team.models import TeamMessage, TeamMessageType
            msg = TeamMessage(
                from_agent=get_current_agent(),
                to_agent=task.assigned_agent,
                msg_type=TeamMessageType.TEXT,
                content=(
                    f"Task [{task_id}] '{task.title}' failed review #{new_revision}.\n"
                    f"Feedback: {feedback}\n"
                    f"Revise and resubmit for review: task_update(task_id=\"{task_id}\", "
                    f"status=\"in_review\", output=\"...\")"
                ),
                task_id=task_id,
            )
            await message_bus.send(msg)
        return (
            f"Requested changes on task [{task_id}] '{task.title}' (revision #{new_revision}).\n"
            f"Feedback: {feedback}"
        )

    # ═════════════════════════════════════════════════════════════════
    # 共享工具
    # ═════════════════════════════════════════════════════════════════

    @tool
    async def task_create(
        title: str, description: str = "", assigned_agent: str = "",
        dependencies: Any = None, priority: str = "medium",
        goal: str = "", background: str = "",
        constraints: Any = None, format: str = "",
        acceptance_criteria: Any = None,
        risk: str = "",
    ) -> str:
        """Create a new task on the Team task board.

        Optionally fill structured fields (goal/background/constraints/format/acceptance_criteria);
        otherwise the plain-text description is used (light-task fallback path).

        Args:
            title: task title; description: detailed description
            assigned_agent: who to assign (empty = auto-assign); dependencies: task IDs to depend on
            priority: "low"|"medium"|"high"|"critical"
            goal: goal; background: background; constraints: list of constraints
            format: output format requirements; acceptance_criteria: list of acceptance criteria
            risk: risk level "low"|"high" (empty = system-inferred: write operations/acceptance criteria/downstream dependents → high)
        """
        if task_store is None:
            return "Error: Task store not available"

        # ── 依赖校验 ──
        dep_list = _normalize_str_list(dependencies)
        dep_warnings = await _check_dependencies(dep_list)

        # ── 组装结构化 spec (全空则降级纯文本) ──
        spec = _build_spec(
            goal=goal, background=background, description=description,
            constraints=constraints, format=format,
            acceptance_criteria=acceptance_criteria,
        )
        full_desc = spec.render() if spec is not None else description

        task = await task_store.create_task(
            title=title, description=full_desc,
            assigned_agent=assigned_agent if assigned_agent else None,
            dependencies=dep_list, priority=priority,
            spec=spec,
        )
        # ── Phase 3: 落定风险等级 + 被依赖任务升级 high ──
        final_risk = await _apply_task_risk(task, risk)
        await _upgrade_dependency_risk(dep_list)
        task = await task_store.get_task(task.id) or task
        # ── SSE: 推送任务创建事件到前端 ──
        await _emit_task_update(task)
        # ── 唤醒 dispatch 循环 ──
        _wake()

        result = (f"Task created:\n- ID: {task.id}\n- Title: {task.title}\n"
                  f"- Status: {task.status}\n- Assigned: {task.assigned_agent or 'unassigned'}"
                  f"\n- Risk level: {final_risk}")
        if dep_list:
            result += f"\n- Dependencies: {', '.join(dep_list)}"
        if dep_warnings:
            result += "\n\n" + "\n".join(dep_warnings)
        return result

    @tool
    async def task_list(status: str = "", assigned_agent: str = "") -> str:
        """Query the Team task board, including dependency blocking status.

        By default only active tasks are shown (pending/in_progress/in_review/revision_needed);
        completed/failed/cancelled tasks are hidden.
        Use status="all" to see all tasks.
        Use status="completed" to see only completed tasks.

        Each task shows:
        - status icon + [ID] title → assignee
        - dependency status: 🔒 blocked (dependencies incomplete) or ✅ dependencies satisfied
        - blocking details: current status of each dependency task

        status filter: pending|in_progress|in_review|completed|failed|cancelled|all
        """
        if task_store is None:
            return "Error: Task store not available"

        # 加载全部任务
        all_tasks = await task_store.load_tasks()

        # ── 过滤 ──
        if status == "all":
            tasks = all_tasks
        elif status:
            status_filter = TeamTaskStatus(status)
            tasks = [t for t in all_tasks if t.status == status_filter]
        else:
            # 默认: 只显示活跃任务 (非终态)
            tasks = [t for t in all_tasks if not t.status.is_terminal]

        if assigned_agent:
            tasks = [t for t in tasks if t.assigned_agent == assigned_agent]

        if not tasks:
            terminal_count = sum(1 for t in all_tasks if t.status.is_terminal)
            msg = "No active tasks on the task board."
            if terminal_count > 0:
                msg += f" ({terminal_count} completed/failed task(s) hidden; use status=\"all\" to view)"
            return msg

        # ── 解析依赖状态 ──
        task_map: dict[str, Any] = {t.id: t for t in all_tasks}
        success_ids = {t.id for t in all_tasks if t.status.is_success}

        icons = {
            "pending": "⏳", "in_progress": "🔄", "in_review": "👁️",
            "revision_needed": "↩️", "approved": "✅", "completed": "✅",
            "failed": "❌", "cancelled": "🚫",
        }

        lines = [f"Total {len(tasks)} tasks:\n"]
        for t in tasks:
            # ── 依赖解析 ──
            blocked = False
            dep_statuses: list[str] = []
            if t.dependencies:
                for dep_id in t.dependencies:
                    dep_task = task_map.get(dep_id)
                    if dep_task is None:
                        dep_statuses.append(f"{dep_id}=missing")
                        blocked = True
                    elif not dep_task.status.is_success:
                        dep_statuses.append(f"{dep_id}={dep_task.status.value}")
                        blocked = True
                    else:
                        dep_statuses.append(f"{dep_id}=✅")

            # ── 阻塞状态标记 ──
            if t.status == TeamTaskStatus.PENDING and blocked:
                blocker = "🔒 Blocked"
            elif t.status == TeamTaskStatus.PENDING and t.dependencies:
                blocker = "✅ Dependencies ready"
            elif t.status == TeamTaskStatus.IN_PROGRESS:
                blocker = "🔄 In progress"
            elif t.status == TeamTaskStatus.IN_REVIEW:
                blocker = "👁️ In review"
            elif t.status == TeamTaskStatus.REVISION_NEEDED:
                blocker = "↩️ Revision needed"
            elif t.status.is_success:
                blocker = "✅ Completed"
            elif t.status in (TeamTaskStatus.FAILED, TeamTaskStatus.CANCELLED):
                blocker = "❌ Terminated"
            else:
                blocker = ""

            # ── 历史任务打标: 任务板 thread 级持久化, 本次 run 前创建的任务
            # 标记"历史", 避免 Lead 把此前 run 的任务计入本次进度 (E2E 观察) ──
            history_tag = ""
            if run_started_at is not None and t.status.is_terminal:
                try:
                    started = run_started_at()
                    if started and t.created_at and t.created_at < started:
                        history_tag = " (history)"
                except Exception:
                    pass

            line = (
                f"- {icons.get(t.status.value, '❓')} [{t.id}] {t.title}"
                f" → {t.assigned_agent or 'unassigned'} | {blocker}{history_tag}"
            )
            lines.append(line)

            if dep_statuses:
                lines.append(f"  Dependencies: {', '.join(dep_statuses)}")

        # ── 汇总 ──
        pending = sum(1 for t in tasks if t.status == TeamTaskStatus.PENDING)
        blocked_count = 0
        for t in tasks:
            if t.status == TeamTaskStatus.PENDING and t.dependencies:
                if not all(dep in success_ids for dep in t.dependencies):
                    blocked_count += 1

        if blocked_count > 0:
            lines.append(
                f"\n⚠️ {blocked_count} task(s) waiting on dependencies (🔒 blocked)"
            )
        ready = pending - blocked_count
        if ready > 0:
            lines.append(f"   {ready} task(s) have dependencies ready, awaiting assignment/execution")

        return "\n".join(lines)

    @tool
    async def task_update(task_id: str, status: str = "", output: str = "", assigned_agent: str = "", result: str = "") -> str:
        """Update the status of the task you are currently executing (Member only).

        You can only update tasks assigned to you (assigned_agent == you).
        Status flow: in_progress → in_review (recommended, submit for acceptance) or completed (finish directly).
        Use status="failed" with a reason on failure.

        When submitting in_review/completed/failed, attach a result JSON:
          {"output": "outcome summary", "evidence": ["evidence: file path/command/link"],
           "uncertainty": "low|medium|high", "failure_reason": "failure reason (only on failure)",
           "skill_feedback": [{"name": "experimental skill name", "success": true}]}
        skill_feedback is reported only when experimental evolution skills were used (optional).
        Light tasks may use output as plain text only; if result is not valid JSON it is treated as plain-text output.

        Acceptance path by task risk (Phase 3):
        - Low-risk tasks: on in_review submission, evidence (file existence) is validated programmatically;
          no evidence or validation passes → completed directly without review; validation fails → Lead review
        - High-risk tasks: after in_review submission, accepted by an independent Verifier or the Lead;
          rejection sends the task back (revision_needed) with review comments

        Note: this is a Member tool; the Lead cannot use it. Lead: use task_review to review tasks.
        """
        if task_store is None:
            return "Error: Task store not available"

        caller = get_current_agent()
        task = await task_store.get_task(task_id)
        if task is None:
            return (
                f"Error: Task '{task_id}' does not exist."
                f"Use task_list to see task IDs on the current task board."
            )

        # ── 守卫: 只有被分配的 Member 可以更新 ──
        if task.assigned_agent and task.assigned_agent != caller:
            return (
                f"Error: Task '{task_id}' is assigned to '{task.assigned_agent}', "
                f"not you ({caller}). You can only update tasks assigned to yourself."
            )

        updates: dict[str, Any] = {}
        if status:
            try:
                new_status = TeamTaskStatus(status)
            except ValueError:
                return (
                    f"Error: Invalid status '{status}'."
                    f"Valid values: in_progress, in_review, completed, failed"
                )
            # ── 守卫: 状态流转校验 ──
            allowed = _allowed_transitions(task.status)
            if new_status not in allowed:
                allowed_str = ", ".join(s.value for s in allowed)
                return (
                    f"Error: Cannot transition directly from '{task.status.value}' to '{status}'."
                    f"Allowed statuses: {allowed_str}"
                )
            # ── 守卫: 高危任务禁止直达 COMPLETED (E2E 观察到 member 直接提交
            # completed 绕过验收) — 必须经 in_review 由 Verifier/Lead 验收 ──
            if new_status == TeamTaskStatus.COMPLETED and not task.verifies_task_id:
                effective_risk = task.risk or infer_task_risk(task)
                if effective_risk == "high":
                    return (
                        "Error: High-risk tasks cannot be marked completed directly; they must first be "
                        "submitted as in_review for independent acceptance (Verifier/Lead)."
                        "Submit with task_update(status=\"in_review\", result=...)."
                    )
            updates["status"] = new_status
        if output:
            updates["output"] = output
        # ── result JSON (Phase 2): 解析成功存结构化 result, 失败降级纯文本 output ──
        if result:
            parsed = _parse_task_result(result, status=status or "")
            if parsed is not None:
                updates["result"] = parsed
                # 同步旧字段, 保证未适配 result 的下游路径仍能读到产出
                if parsed.output and "output" not in updates:
                    updates["output"] = parsed.output
                if parsed.failure_reason and not task.error:
                    updates["error"] = parsed.failure_reason
            elif "output" not in updates:
                # 非法 JSON → 整个字符串按纯文本 output 处理 (现状行为)
                updates["output"] = result
        if assigned_agent:
            return "Error: task_update cannot modify assigned_agent. Use delegate_to_member instead."
        if not updates:
            return "No update fields provided. Provide at least status or output."

        # ── Phase 3: 低危任务快速通道 — 提交 in_review 时程序校验证据, 通过即直通 COMPLETED ──
        # fail-safe 方向: 证据缺失/无法校验 → 保持 IN_REVIEW 转 Lead 审查;
        # uncertainty 仅展示, 不参与直通判断 (决策表 §0-3)。
        fast_track_note = ""
        if updates.get("status") == TeamTaskStatus.IN_REVIEW:
            effective_risk = task.risk or infer_task_risk(task)
            if effective_risk == "low":
                submitted = updates.get("result") or task.result
                evidence = list(submitted.evidence) if submitted is not None else []
                if not evidence:
                    updates["status"] = TeamTaskStatus.COMPLETED
                    fast_track_note = "\nLow-risk task, no evidence to validate → completed directly (no review)."
                else:
                    ok, missing = _validate_evidence(
                        evidence, _evidence_workspace_roots(caller),
                    )
                    if ok:
                        updates["status"] = TeamTaskStatus.COMPLETED
                        fast_track_note = "\nLow-risk task, evidence validation passed → completed directly (no review)."
                    else:
                        fast_track_note = (
                            f"\n⚠️ Evidence validation failed (files not found: {', '.join(missing)}), "
                            f"escalated to Lead review."
                        )

        updated = await task_store.update_task(task_id, **updates)
        if updated is None:
            return f"Error: Failed to update task '{task_id}'"
        # ── Phase 5: 试验性技能使用上报 → record_use (best-effort, 不上报不计) ──
        _result_obj = updates.get("result")
        _feedback = (
            getattr(_result_obj, "skill_feedback", None)
            if _result_obj is not None else None
        )
        if _feedback:
            instance = get_current_agent_instance()
            _evo_store = (
                getattr(instance, "_skill_evolution_store", None)
                if instance is not None else None
            )
            if _evo_store is not None:
                for fb in _feedback:
                    try:
                        _evo_store.record_use(caller, fb.name, fb.success)
                    except Exception:
                        logger.debug(
                            "skill record_use failed (%s/%s)", caller, fb.name,
                            exc_info=True,
                        )
        # ── SSE: 推送任务更新事件到前端 ──
        await _emit_task_update(updated)
        # ── 唤醒 dispatch 循环 ──
        _wake()
        return f"Task [{task_id}] updated: {updated.title} → {updated.status.value}{fast_track_note}"

    @tool
    async def send_message(to_agent: str, content: str, task_id: str = "") -> str:
        """Send a message to another Agent in the Team."""
        if message_bus is None:
            return "Error: Message bus not available"
        from harness.team.models import TeamMessage, TeamMessageType
        msg = TeamMessage(
            from_agent=get_current_agent(), to_agent=to_agent,
            msg_type=TeamMessageType.TEXT, content=content,
            task_id=task_id if task_id else None,
        )
        await message_bus.send(msg)
        return f"Message sent to '{to_agent}'"

    @tool
    async def read_inbox() -> str:
        """Read your own inbox (drain-on-read)."""
        if message_bus is None:
            return "Error: Message bus not available"
        messages = await message_bus.read_inbox(get_current_agent())
        if not messages:
            return "Inbox is empty."
        # 协议消息必须与 InboxDrainMiddleware 走同一条结构化路由 — 否则 request_id 丢失,
        # 协议状态机不登记, LLM 无法正确调用 shutdown_response/approve_plan
        instance = get_current_agent_instance()
        lines = [f"Total {len(messages)} new messages:\n"]
        for msg in messages:
            if msg.msg_type in _PROTOCOL_MESSAGE_TYPES and instance is not None:
                await instance._handle_inbox_message(msg)
                # 附上类型/request_id/内容摘要 — 否则当前轮 LLM 看不到计划内容,
                # 也拿不到 request_id, 无法本轮调用 approve_plan/shutdown_response
                lines.append(
                    f"- [protocol:{msg.msg_type.value}] from **{msg.from_agent}** "
                    f"(request_id={msg.request_id or 'none'}): {msg.content[:300]}\n"
                    f"  → handled per protocol; injected context takes effect next turn."
                )
            else:
                lines.append(f"- [{msg.msg_type.value}] from **{msg.from_agent}**: {msg.content[:200]}")
        return "\n".join(lines)

    @tool
    async def broadcast(content: str, task_id: str = "") -> str:
        """Broadcast a message to all Team members."""
        if message_bus is None:
            return "Error: Message bus not available"
        from harness.team.models import TeamMessage, TeamMessageType
        msg = TeamMessage(
            from_agent=get_current_agent(), to_agent=None,
            msg_type=TeamMessageType.BROADCAST, content=content,
            task_id=task_id if task_id else None,
        )
        await message_bus.send(msg)
        return "Broadcast message sent to all members."

    # ═════════════════════════════════════════════════════════════════
    # Member 专属工具
    # ═════════════════════════════════════════════════════════════════

    @tool
    async def request_plan_approval(plan_description: str) -> str:
        """ Request approval from the Lead for a high-risk operation plan."""
        if message_bus is None:
            return "Error: Message bus not available"
        from harness.team.models import RequestStatus, TeamMessage, TeamMessageType
        # 定向发给 Lead — to_agent=None 是广播语义, 会唤醒所有 member 空转 (他们没有 approve_plan 工具)
        target = lead_name or getattr(get_current_agent_instance(), "_lead_name", None)
        if not target:
            return "Error: Cannot determine the Lead Agent; approval request not sent."
        req_id = str(_uuid.uuid4())[:8]
        msg = TeamMessage(
            from_agent=get_current_agent(), to_agent=target,
            msg_type=TeamMessageType.PLAN_APPROVAL_REQUEST,
            content=plan_description, request_id=req_id,
        )
        await message_bus.send(msg)
        # ── 登记协议追踪 (发起方) — 否则审批结果回来时 _pending_requests 永不命中 ──
        agent = get_current_agent_instance()
        if agent is not None:
            async with agent._tracker_lock:
                agent._pending_requests[req_id] = {
                    "type": "plan_approval",
                    "status": RequestStatus.PENDING,
                    "from": get_current_agent(),
                    "plan": plan_description,
                }
        return (
            f"Approval request sent to Lead '{target}' (req_id={req_id}).\n"
            f"**Stop this turn immediately**: do not perform any pending operations; "
            f"wait for the Lead's approval reply (plan_approval_response) to wake you before continuing."
        )

    @tool
    async def shutdown_response(request_id: str, requester: str, approve: bool, reason: str = "") -> str:
        """Respond to a shutdown request — structured handshake.

        After receiving a shutdown_request, the LLM decides whether to approve the shutdown:
        - approve=True: approve; the Agent exits gracefully after the current turn
        - approve=False: reject; continue the current task

        Args:
            request_id: ID of the shutdown request (must match the received request)
            requester: name of the Agent that initiated the shutdown request
            approve: whether to approve the shutdown
            reason: rejection reason (recommended when approve=False)
        """
        if message_bus is None:
            return "Error: Message bus not available"
        from harness.team.models import TeamMessage, TeamMessageType

        status_text = "approved" if approve else f"rejected: {reason}" if reason else "rejected"
        msg = TeamMessage(
            from_agent=get_current_agent(), to_agent=requester,
            msg_type=TeamMessageType.SHUTDOWN_RESPONSE,
            content=status_text, request_id=request_id,
            approved=approve,  # 结构化结果 — 接收方优先读此字段
        )
        await message_bus.send(msg)

        if approve:
            agent = get_current_agent_instance()
            if agent is not None:
                agent._should_exit = True
                async with agent._tracker_lock:
                    agent._pending_requests[request_id] = {
                        "type": "shutdown", "status": "approved", "from": requester,
                    }
            return f"Shutdown request approved (req_id={request_id}). The Agent will exit after the current task completes."

        # 拒绝: 更新追踪器 (加锁, 与 _handle_inbox_message 的加锁写对齐)
        agent = get_current_agent_instance()
        if agent is not None:
            async with agent._tracker_lock:
                agent._pending_requests[request_id] = {
                    "type": "shutdown", "status": "rejected", "from": requester, "reason": reason,
                }
        return f"Shutdown request rejected (req_id={request_id}). Continuing the current task."

    # ═════════════════════════════════════════════════════════════════
    # memory_search — 按需查询任务记忆详情
    # ═════════════════════════════════════════════════════════════════

    @tool
    async def memory_search(
        query: str = "",
        task_id: str = "",
        max_results: int = 5,
    ) -> str:
        """Search memories of completed tasks (decisions, pitfalls, discoveries) by keyword or task ID.

        When the `<task_memory>` compressed digest injected into the prompt is not enough to
        understand the full background, use this tool to get the details.
        Each task memory contains: summary, decisions, pitfalls, discoveries, tags.

        —— WHEN TO USE ——
        - A compressed digest mentions a relevant task, but you need the details of its decisions/pitfalls
        - The current task hit an error and you want to check whether past tasks hit it too
        - You want to know how a technical approach was decided in past tasks

        Args:
            query: search keywords (matched against summary, tags, title). Empty returns the most recent completed tasks.
            task_id: exact lookup by task ID. Takes precedence over query when both are provided.
            max_results: maximum number of results (1-10, default 5)
        """
        # 获取当前 agent 实例以访问 project_id / user_id
        agent = get_current_agent_instance()
        if agent is None:
            return "Error: Cannot get the current Agent context"

        project_id = agent._project_id if hasattr(agent, "_project_id") else ""
        user_id = agent._user_id if hasattr(agent, "_user_id") else "default"

        if not project_id:
            return "Error: project_id is not set; cannot access task memories"

        from harness.memory.task_memory import TaskMemoryStore

        store = TaskMemoryStore(project_id=project_id, user_id=user_id)

        # 精确查询
        if task_id:
            memory = await store.load(task_id)
            if memory is None:
                return f"No memory found for task '{task_id}'. The task may not have extracted memory yet, or the task ID does not exist."
            return _format_memory_detail(memory)

        max_results = max(1, min(max_results, 10))

        # 关键词搜索
        all_memories = await store.list_all()
        if not all_memories:
            return "No task memories extracted yet. Memories are extracted automatically when tasks complete."

        results: list = []
        if query:
            query_lower = query.lower()
            keywords: set[str] = set()
            for kw in query_lower.split():
                kw = kw.strip()
                if len(kw) >= 2:
                    keywords.add(kw)

            if keywords:
                scored: list[tuple[int, Any]] = []
                for m in all_memories:
                    text = (
                        f"{m.summary} {m.task_title} {' '.join(m.tags)}"
                    ).lower()
                    score = sum(1 for kw in keywords if kw in text)
                    if score > 0:
                        scored.append((score, m))
                scored.sort(key=lambda x: -x[0])
                results = [m for _, m in scored[:max_results]]
        else:
            results = all_memories[:max_results]

        if not results:
            return (
                f"No task memories related to '{query}' found."
                f"Try different keywords, or wait for more tasks to complete so memories are extracted automatically."
            )

        parts = [f"Found {len(results)} related task memories:\n"]
        for m in results:
            parts.append(_format_memory_detail(m))
            parts.append("")
        return "\n---\n\n".join(parts)

    # ═════════════════════════════════════════════════════════════════
    # 按角色组装
    # ═════════════════════════════════════════════════════════════════

    all_tools: dict[str, BaseTool] = {
        # Lead 专属
        "delegate_to_member": delegate_to_member,
        "list_teammates": list_teammates,
        "shutdown_teammate": shutdown_teammate,
        "approve_plan": approve_plan,
        "spawn_teammate": spawn_teammate,
        "task_review": task_review,
        # 共享
        "task_create": task_create,
        "task_list": task_list,
        "send_message": send_message,
        "read_inbox": read_inbox,
        "broadcast": broadcast,
        "memory_search": memory_search,
        # Member 专属
        "task_update": task_update,
        "request_plan_approval": request_plan_approval,
        "shutdown_response": shutdown_response,
    }

    if role == "lead":
        allowed = LEAD_TOOLS | SHARED_TOOLS
    else:
        allowed = SHARED_TOOLS | MEMBER_TOOLS

    result = [t for name, t in all_tools.items() if name in allowed]
    logger.info("create_team_tools: role=%s → %d tools: %s", role, len(result), sorted(allowed))
    return result
