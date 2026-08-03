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
        f"执行者: {memory.assigned_agent or '未知'} | 状态: {memory.status}",
    ]
    if memory.summary:
        lines.append(f"摘要: {memory.summary}")
    if memory.decisions:
        lines.append("决策:")
        lines.extend(f"  - {d}" for d in memory.decisions)
    if memory.pitfalls:
        lines.append("踩坑:")
        lines.extend(f"  - {p}" for p in memory.pitfalls)
    if memory.discoveries:
        lines.append("发现:")
        lines.extend(f"  - {d}" for d in memory.discoveries)
    if memory.tags:
        lines.append(f"标签: {', '.join(memory.tags)}")
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
        f"\n\n[提交要求]\n"
        f"完成后请用 task_update 提交审查: "
        f"task_update(task_id=\"{task_id}\", status=\"in_review\", result={{...}})。\n"
        f"result 为 JSON 对象, 字段:\n"
        f'- output: 成果总结 (必填)\n'
        f'- evidence: 证据列表 (文件路径/命令/链接, 可空)\n'
        f'- uncertainty: 自评不确定性 "low"|"medium"|"high" (默认 low, 仅供参考)\n'
        f'- failure_reason: 失败原因 (status="failed" 时必填)\n'
        f"轻任务可只填 output; 失败时 status=\"failed\" 并填 failure_reason。\n"
        f"[验收路径] 低风险任务: 证据校验通过即直接完成 (免审查); "
        f"高风险任务: 由独立 Verifier 或 Lead 审查, 不通过会打回返工。"
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
                dep_warnings.append(f"⚠️ 依赖 '{dep_id}' 不存在")
            elif dep_task.status in (TeamTaskStatus.FAILED, TeamTaskStatus.CANCELLED):
                dep_warnings.append(
                    f"⚠️ 依赖 '{dep_id}' ({dep_task.title}) 已处于终态 "
                    f"'{dep_task.status.value}', 当前任务将永远被阻塞"
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
            return "high (程序复核: Lead 标记 low 但命中写操作信号, 已升级)"
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
        """创建任务并委派给指定 Team Member Agent 执行 (Lead 专属, 一步完成).

        任务创建后立即分配给目标成员, orchestrator 的 dispatch 循环会自动派单。
        任务必须自包含: 成员看不到你的对话历史, 请把背景/目标/约束写全。
        只提供 description 纯文本也可以 (轻任务降级路径), 但复杂任务建议填结构化字段。

        验收路径按风险分级 (Phase 3):
        - 低风险 (只读/探索/查询): 成员提交后程序校验证据, 通过即直接完成 (免审查)
        - 高风险 (写操作/有验收标准/有下游依赖): 强制验收 — 团队有 Verifier 成员时
          自动创建独立验收子任务; 无 Verifier 时由你用 task_review 审查。
        可用 risk 参数显式指定等级; 不指定则由系统按规则推断。

        Args:
            agent_name: 目标 Member Agent 名称
            title: 任务标题
            goal: 目标 (交付什么)
            background: 背景 (为什么做, 必要的上下文原文)
            description: 详细描述 (未填结构化字段时作为纯文本任务描述)
            constraints: 约束/注意事项列表 (技术栈/边界/禁止事项)
            format: 输出格式要求
            acceptance_criteria: 验收标准列表
            dependencies: 依赖的任务 ID 列表 (依赖完成后才派单)
            priority: "low"|"medium"|"high"|"critical"
            risk: 风险等级 "low"|"high" (留空=系统推断: 写操作/验收标准/下游依赖 → high)
        """
        if task_store is None:
            return "Error: Task store not available"

        # ── 成员存在性检查 (懒加载: 名册 + 已 spawn 都算在团队中) ──
        member_warning = ""
        known = set(teammates.keys() if teammates else ()) | set(member_names or ())
        if known and agent_name not in known:
            member_warning = (
                f"\n⚠️ 警告: 成员 '{agent_name}' 不在当前团队中。"
                f"可用成员: {', '.join(sorted(known))}"
                f"\n任务仍会创建, 但需要手动调整分配或等成员加入。"
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
            f"已创建并委派任务 [{task.id}] 给 '{agent_name}'。\n"
            f"任务标题: {title}\n"
            f"优先级: {priority}\n"
            f"风险等级: {final_risk} ({'低风险: 证据校验通过即直通' if final_risk == 'low' else '高风险: 强制独立验收'})"
        )
        if spec is not None and spec.goal:
            result += f"\n目标: {spec.goal[:200]}"
        if dep_list:
            result += f"\n依赖: {', '.join(dep_list)}"
        if dep_warnings:
            result += "\n\n" + "\n".join(dep_warnings)
        return result + member_warning

    @tool
    async def list_teammates() -> str:
        """查看 Team 中所有 Member 的当前状态 (Lead 专属).

        只列出 Member Agent, 不包含 Lead 自身。
        """
        if teammates is None:
            return "Teammate 列表不可用."
        members = {
            name: tm for name, tm in teammates.items()
            if getattr(tm, "_role", "") != "lead"
        }
        # ── 懒加载: 名册中未 spawn 的成员也列出 (语义上可用, 派单时自动拉起) ──
        standby = [n for n in (member_names or []) if n not in members]
        if not members and not standby:
            return "当前 Team 中没有 Member (仅 Lead)。"
        lines = [f"共 {len(members) + len(standby)} 个 Member:\n"]
        for name, tm in members.items():
            icon = {"idle": "🟢", "working": "🔵", "failed": "❌"}.get(
                tm.status.value if hasattr(tm.status, 'value') else str(tm.status), "❓")
            task_info = f" (任务: {tm.current_task_id})" if tm.current_task_id else ""
            lines.append(f"- {icon} **{name}** [{tm.status}] — 完成 {tm.completed_tasks}{task_info}")
        for name in standby:
            lines.append(f"- ⚪ **{name}** [standby] — 待拉起 (派单时自动启动)")
        return "\n".join(lines)

    @tool
    async def shutdown_teammate(agent_name: str) -> str:
        """ 请求关闭指定 teammate (Lead 专属)."""
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
        return f"已向 '{agent_name}' 发送关闭请求 (req_id={req_id})"

    @tool
    async def approve_plan(request_id: str, requester: str, approve: bool, feedback: str = "") -> str:
        """审批 Teammate 提交的计划 —  结构化审批 (Lead 专属).

        收到 plan_approval_request 后, 审阅计划内容并用此工具回复:
        - approve=True: 批准计划, Teammate 将继续执行
        - approve=False: 拒绝计划, Teammate 需要调整后重新提交

        Args:
            request_id: 计划审批请求的 ID (必须与收到的请求匹配)
            requester: 提交计划的 Agent 名称
            approve: 是否批准计划
            feedback: 审批反馈 (批准时可提供补充建议, 拒绝时必须说明原因)
        """
        if message_bus is None:
            return "Error: Message bus not available"
        from harness.team.models import TeamMessage, TeamMessageType

        status_text = f"approved. {feedback}" if approve else f"rejected: {feedback or '计划未通过审批'}"
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

        action = "批准" if approve else "拒绝"
        return f"已{action}来自 '{requester}' 的计划 (req_id={request_id})。"

    @tool
    async def spawn_teammate(agent_name: str) -> str:
        """动态创建并启动一个新的 Teammate Agent (Lead 专属).

        当你需要扩充团队时调用此工具。新 teammate 将:
        1. 使用其预配置的 SOUL.md 作为 system prompt
        2. 自动进入 IDLE 状态, 等待任务分配
        3. 支持  自主认领任务板上的未分配任务

        Args:
            agent_name: 要创建的 Agent 名称 (必须在 agents 配置中存在)
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
        """审查成员提交的任务 (Lead 专属).

        当成员完成任务并调用 task_update(status="in_review") 提交审查后,
        你应审阅其 output 并决定通过还是要求修改。

        Args:
            task_id: 要审查的任务 ID
            approve: True=通过, 任务变为 approved (终态)
            feedback: 审查意见。通过时可附简要评价; 要求修改时必须写清具体的修改要求,
                      让成员明确知道要改什么。
        """
        if task_store is None:
            return "Error: Task store not available"

        task = await task_store.get_task(task_id)
        if task is None:
            return f"Error: Task '{task_id}' not found"
        if task.status != TeamTaskStatus.IN_REVIEW:
            return (
                f"Error: Task '{task_id}' 当前状态为 '{task.status.value}', "
                f"不是 'in_review', 无法审查。只有成员通过 task_update(status=\"in_review\") "
                f"提交的任务才能审查。"
            )

        if approve:
            await task_store.update_task(
                task_id,
                status=TeamTaskStatus.APPROVED,
                review_feedback=feedback or "已通过",
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
                        f"任务 [{task_id}] '{task.title}' 审查通过 ✅"
                        + (f" — {feedback}" if feedback else "")
                    ),
                    task_id=task_id,
                )
                await message_bus.send(msg)
            return (
                f"已通过任务 [{task_id}] '{task.title}'。"
                + (f" 评价: {feedback}" if feedback else "")
            )

        # ── 要求修改 ──
        if not feedback:
            return "Error: 要求修改时必须提供 feedback 说明具体需要修改什么。"
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
                    f"任务 [{task_id}] '{task.title}' 第 {new_revision} 次审查不通过。\n"
                    f"反馈: {feedback}\n"
                    f"请修改后重新提交审查: task_update(task_id=\"{task_id}\", "
                    f"status=\"in_review\", output=\"...\")"
                ),
                task_id=task_id,
            )
            await message_bus.send(msg)
        return (
            f"已要求修改任务 [{task_id}] '{task.title}' (第 {new_revision} 次)。\n"
            f"反馈: {feedback}"
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
        """在 Team 任务板上创建新任务.

        可选填结构化字段 (goal/background/constraints/format/acceptance_criteria),
        不填则按纯文本 description 处理 (轻任务降级路径)。

        Args:
            title: 任务标题; description: 详细描述
            assigned_agent: 分配给谁 (留空=自动分配); dependencies: 依赖的任务 ID
            priority: "low"|"medium"|"high"|"critical"
            goal: 目标; background: 背景; constraints: 约束列表
            format: 输出格式要求; acceptance_criteria: 验收标准列表
            risk: 风险等级 "low"|"high" (留空=系统推断: 写操作/验收标准/下游依赖 → high)
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

        result = (f"任务已创建:\n- ID: {task.id}\n- 标题: {task.title}\n"
                  f"- 状态: {task.status}\n- 分配: {task.assigned_agent or '待分配'}"
                  f"\n- 风险等级: {final_risk}")
        if dep_list:
            result += f"\n- 依赖: {', '.join(dep_list)}"
        if dep_warnings:
            result += "\n\n" + "\n".join(dep_warnings)
        return result

    @tool
    async def task_list(status: str = "", assigned_agent: str = "") -> str:
        """查询 Team 任务板，含依赖阻塞状态。

        默认只显示活跃任务 (pending/in_progress/in_review/revision_needed),
        隐藏已完成/失败/取消的任务。
        使用 status="all" 查看全部任务。
        使用 status="completed" 只查看已完成的任务。

        每个任务会显示:
        - 状态图标 + [ID] 标题 → 分配对象
        - 依赖状态: 🔒 阻塞中 (依赖未完成) 或 ✅ 依赖已满足
        - 阻塞详情: 每个依赖任务的当前状态

        status 过滤: pending|in_progress|in_review|completed|failed|cancelled|all
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
            msg = "任务板无活跃任务。"
            if terminal_count > 0:
                msg += f" ({terminal_count} 个已完成/失败的任务已隐藏, 用 status=\"all\" 查看)"
            return msg

        # ── 解析依赖状态 ──
        task_map: dict[str, Any] = {t.id: t for t in all_tasks}
        success_ids = {t.id for t in all_tasks if t.status.is_success}

        icons = {
            "pending": "⏳", "in_progress": "🔄", "in_review": "👁️",
            "revision_needed": "↩️", "approved": "✅", "completed": "✅",
            "failed": "❌", "cancelled": "🚫",
        }

        lines = [f"共 {len(tasks)} 个任务:\n"]
        for t in tasks:
            # ── 依赖解析 ──
            blocked = False
            dep_statuses: list[str] = []
            if t.dependencies:
                for dep_id in t.dependencies:
                    dep_task = task_map.get(dep_id)
                    if dep_task is None:
                        dep_statuses.append(f"{dep_id}=不存在")
                        blocked = True
                    elif not dep_task.status.is_success:
                        dep_statuses.append(f"{dep_id}={dep_task.status.value}")
                        blocked = True
                    else:
                        dep_statuses.append(f"{dep_id}=✅")

            # ── 阻塞状态标记 ──
            if t.status == TeamTaskStatus.PENDING and blocked:
                blocker = "🔒 阻塞中"
            elif t.status == TeamTaskStatus.PENDING and t.dependencies:
                blocker = "✅ 依赖就绪"
            elif t.status == TeamTaskStatus.IN_PROGRESS:
                blocker = "🔄 执行中"
            elif t.status == TeamTaskStatus.IN_REVIEW:
                blocker = "👁️ 审查中"
            elif t.status == TeamTaskStatus.REVISION_NEEDED:
                blocker = "↩️ 需修改"
            elif t.status.is_success:
                blocker = "✅ 已完成"
            elif t.status in (TeamTaskStatus.FAILED, TeamTaskStatus.CANCELLED):
                blocker = "❌ 已终止"
            else:
                blocker = ""

            # ── 历史任务打标: 任务板 thread 级持久化, 本次 run 前创建的任务
            # 标记"历史", 避免 Lead 把此前 run 的任务计入本次进度 (E2E 观察) ──
            history_tag = ""
            if run_started_at is not None and t.status.is_terminal:
                try:
                    started = run_started_at()
                    if started and t.created_at and t.created_at < started:
                        history_tag = " (历史)"
                except Exception:
                    pass

            line = (
                f"- {icons.get(t.status.value, '❓')} [{t.id}] {t.title}"
                f" → {t.assigned_agent or '未分配'} | {blocker}{history_tag}"
            )
            lines.append(line)

            if dep_statuses:
                lines.append(f"  依赖: {', '.join(dep_statuses)}")

        # ── 汇总 ──
        pending = sum(1 for t in tasks if t.status == TeamTaskStatus.PENDING)
        blocked_count = 0
        for t in tasks:
            if t.status == TeamTaskStatus.PENDING and t.dependencies:
                if not all(dep in success_ids for dep in t.dependencies):
                    blocked_count += 1

        if blocked_count > 0:
            lines.append(
                f"\n⚠️ {blocked_count} 个任务正在等待依赖完成 (🔒 阻塞中)"
            )
        ready = pending - blocked_count
        if ready > 0:
            lines.append(f"   {ready} 个任务依赖已就绪, 等待分配/执行")

        return "\n".join(lines)

    @tool
    async def task_update(task_id: str, status: str = "", output: str = "", assigned_agent: str = "", result: str = "") -> str:
        """更新你当前执行的任务状态 (Member 专属).

        只能更新你正在执行的任务 (assigned_agent == 你)。
        状态流转: in_progress → in_review (推荐, 提交验收) 或 completed (直接完成).
        失败时使用 status="failed" 并说明原因.

        提交 in_review/completed/failed 时建议附带 result JSON:
          {"output": "成果总结", "evidence": ["证据: 文件路径/命令/链接"],
           "uncertainty": "low|medium|high", "failure_reason": "失败原因(仅失败时)",
           "skill_feedback": [{"name": "试验性技能名", "success": true}]}
        skill_feedback 仅在使用了试验性进化技能时上报 (可选)。
        轻任务可只用 output 纯文本; result 不是合法 JSON 时按纯文本 output 处理。

        验收路径按任务风险分级 (Phase 3):
        - 低风险任务: 提交 in_review 时程序自动校验证据 (文件存在性),
          无证据或校验通过 → 直接 completed 免审查; 校验失败 → 转 Lead 审查
        - 高风险任务: 提交 in_review 后由独立 Verifier 或 Lead 验收,
          不通过会打回 (revision_needed) 并附验收意见

        注意: 这是 Member 工具, Lead 不能使用。Lead 请用 task_review 审查任务。
        """
        if task_store is None:
            return "Error: Task store not available"

        caller = get_current_agent()
        task = await task_store.get_task(task_id)
        if task is None:
            return (
                f"Error: 任务 '{task_id}' 不存在。"
                f"请用 task_list 查看当前任务板上的任务 ID。"
            )

        # ── 守卫: 只有被分配的 Member 可以更新 ──
        if task.assigned_agent and task.assigned_agent != caller:
            return (
                f"Error: 任务 '{task_id}' 分配给了 '{task.assigned_agent}', "
                f"不是你 ({caller})。你只能更新分配给你自己的任务。"
            )

        updates: dict[str, Any] = {}
        if status:
            try:
                new_status = TeamTaskStatus(status)
            except ValueError:
                return (
                    f"Error: 无效的状态 '{status}'。"
                    f"有效值: in_progress, in_review, completed, failed"
                )
            # ── 守卫: 状态流转校验 ──
            allowed = _allowed_transitions(task.status)
            if new_status not in allowed:
                allowed_str = ", ".join(s.value for s in allowed)
                return (
                    f"Error: 不能从 '{task.status.value}' 直接转到 '{status}'。"
                    f"允许的状态: {allowed_str}"
                )
            # ── 守卫: 高危任务禁止直达 COMPLETED (E2E 观察到 member 直接提交
            # completed 绕过验收) — 必须经 in_review 由 Verifier/Lead 验收 ──
            if new_status == TeamTaskStatus.COMPLETED and not task.verifies_task_id:
                effective_risk = task.risk or infer_task_risk(task)
                if effective_risk == "high":
                    return (
                        "Error: 高风险任务不允许直接标记 completed, 必须先提交 "
                        "in_review 接受独立验收 (Verifier/Lead)。"
                        "请用 task_update(status=\"in_review\", result=...) 提交。"
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
            return "Error: 不允许通过 task_update 修改 assigned_agent。请使用 delegate_to_member。"
        if not updates:
            return "未提供任何更新字段。请至少提供 status 或 output。"

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
                    fast_track_note = "\n低风险任务, 无证据需校验 → 已直接完成 (免审查)。"
                else:
                    ok, missing = _validate_evidence(
                        evidence, _evidence_workspace_roots(caller),
                    )
                    if ok:
                        updates["status"] = TeamTaskStatus.COMPLETED
                        fast_track_note = "\n低风险任务, 证据校验通过 → 已直接完成 (免审查)。"
                    else:
                        fast_track_note = (
                            f"\n⚠️ 证据校验未通过 (文件不存在: {', '.join(missing)}), "
                            f"已转 Lead 审查。"
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
        return f"任务 [{task_id}] 已更新: {updated.title} → {updated.status.value}{fast_track_note}"

    @tool
    async def send_message(to_agent: str, content: str, task_id: str = "") -> str:
        """向 Team 中的另一个 Agent 发送消息."""
        if message_bus is None:
            return "Error: Message bus not available"
        from harness.team.models import TeamMessage, TeamMessageType
        msg = TeamMessage(
            from_agent=get_current_agent(), to_agent=to_agent,
            msg_type=TeamMessageType.TEXT, content=content,
            task_id=task_id if task_id else None,
        )
        await message_bus.send(msg)
        return f"消息已发送给 '{to_agent}'"

    @tool
    async def read_inbox() -> str:
        """读取自己的收件箱 (drain-on-read)."""
        if message_bus is None:
            return "Error: Message bus not available"
        messages = await message_bus.read_inbox(get_current_agent())
        if not messages:
            return "收件箱为空."
        # 协议消息必须与 InboxDrainMiddleware 走同一条结构化路由 — 否则 request_id 丢失,
        # 协议状态机不登记, LLM 无法正确调用 shutdown_response/approve_plan
        instance = get_current_agent_instance()
        lines = [f"共 {len(messages)} 条新消息:\n"]
        for msg in messages:
            if msg.msg_type in _PROTOCOL_MESSAGE_TYPES and instance is not None:
                await instance._handle_inbox_message(msg)
                # 附上类型/request_id/内容摘要 — 否则当前轮 LLM 看不到计划内容,
                # 也拿不到 request_id, 无法本轮调用 approve_plan/shutdown_response
                lines.append(
                    f"- [协议:{msg.msg_type.value}] 来自 **{msg.from_agent}** "
                    f"(request_id={msg.request_id or '无'}): {msg.content[:300]}\n"
                    f"  → 已按协议处理, 注入的上下文将在下一轮生效。"
                )
            else:
                lines.append(f"- [{msg.msg_type.value}] 来自 **{msg.from_agent}**: {msg.content[:200]}")
        return "\n".join(lines)

    @tool
    async def broadcast(content: str, task_id: str = "") -> str:
        """向 Team 全体成员广播消息."""
        if message_bus is None:
            return "Error: Message bus not available"
        from harness.team.models import TeamMessage, TeamMessageType
        msg = TeamMessage(
            from_agent=get_current_agent(), to_agent=None,
            msg_type=TeamMessageType.BROADCAST, content=content,
            task_id=task_id if task_id else None,
        )
        await message_bus.send(msg)
        return "广播消息已发送给全体成员。"

    # ═════════════════════════════════════════════════════════════════
    # Member 专属工具
    # ═════════════════════════════════════════════════════════════════

    @tool
    async def request_plan_approval(plan_description: str) -> str:
        """ 向 Lead 请求审批高风险操作计划."""
        if message_bus is None:
            return "Error: Message bus not available"
        from harness.team.models import RequestStatus, TeamMessage, TeamMessageType
        # 定向发给 Lead — to_agent=None 是广播语义, 会唤醒所有 member 空转 (他们没有 approve_plan 工具)
        target = lead_name or getattr(get_current_agent_instance(), "_lead_name", None)
        if not target:
            return "Error: 无法确定 Lead Agent, 审批请求未发送。"
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
            f"审批请求已发送给 Lead '{target}' (req_id={req_id})。\n"
            f"**立即停止本轮工作**: 不要执行任何待审批操作, "
            f"等待 Lead 的审批回复 (plan_approval_response) 唤醒你后再继续。"
        )

    @tool
    async def shutdown_response(request_id: str, requester: str, approve: bool, reason: str = "") -> str:
        """响应关机请求 —  结构化握手.

        收到 shutdown_request 后, 由 LLM 决策是否批准关机:
        - approve=True: 批准关机, Agent 将在当前轮次结束后优雅退出
        - approve=False: 拒绝关机, 继续执行当前任务

        Args:
            request_id: 关机请求的 ID (必须与收到的请求匹配)
            requester: 发起关机请求的 Agent 名称
            approve: 是否批准关机
            reason: 拒绝原因 (approve=False 时建议提供)
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
            return f"已批准关机请求 (req_id={request_id})。Agent 将在当前任务完成后退出。"

        # 拒绝: 更新追踪器 (加锁, 与 _handle_inbox_message 的加锁写对齐)
        agent = get_current_agent_instance()
        if agent is not None:
            async with agent._tracker_lock:
                agent._pending_requests[request_id] = {
                    "type": "shutdown", "status": "rejected", "from": requester, "reason": reason,
                }
        return f"已拒绝关机请求 (req_id={request_id})。继续执行当前任务。"

    # ═════════════════════════════════════════════════════════════════
    # memory_search — 按需查询任务记忆详情
    # ═════════════════════════════════════════════════════════════════

    @tool
    async def memory_search(
        query: str = "",
        task_id: str = "",
        max_results: int = 5,
    ) -> str:
        """搜索已完成任务的记忆（决策、踩坑、发现），使用关键词或任务ID查询。

        当注入到 prompt 的 `<task_memory>` 压缩摘要不足以理解完整背景时，
        使用此工具获取详细内容。每个任务记忆包含:摘要、决策、踩坑、发现、标签。

        —— WHEN TO USE ——
        - 压缩摘要中提到了一个相关任务，但需要看具体决策/踩坑的细节
        - 当前任务遇到一个错误，想查历史任务是否也遇到过
        - 想了解某个技术方案在历史任务中是怎么决策的

        Args:
            query: 搜索关键词（匹配摘要、标签、标题）。为空时返回最近完成的任务。
            task_id: 指定任务ID精确查询。与query同时提供时，task_id优先。
            max_results: 最多返回几条结果 (1-10, 默认5)
        """
        # 获取当前 agent 实例以访问 project_id / user_id
        agent = get_current_agent_instance()
        if agent is None:
            return "Error: 无法获取当前 Agent 上下文"

        project_id = agent._project_id if hasattr(agent, "_project_id") else ""
        user_id = agent._user_id if hasattr(agent, "_user_id") else "default"

        if not project_id:
            return "Error: 未设置 project_id，无法访问任务记忆"

        from harness.memory.task_memory import TaskMemoryStore

        store = TaskMemoryStore(project_id=project_id, user_id=user_id)

        # 精确查询
        if task_id:
            memory = await store.load(task_id)
            if memory is None:
                return f"未找到任务 '{task_id}' 的记忆。该任务可能尚未提取记忆，或任务ID不存在。"
            return _format_memory_detail(memory)

        max_results = max(1, min(max_results, 10))

        # 关键词搜索
        all_memories = await store.list_all()
        if not all_memories:
            return "暂无已提取的任务记忆。当任务完成后，系统会自动提取记忆。"

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
                f"未找到与 '{query}' 相关的任务记忆。"
                f"可尝试不同关键词，或等更多任务完成后系统自动提取记忆。"
            )

        parts = [f"找到 {len(results)} 条相关任务记忆:\n"]
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
