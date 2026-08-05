"""Agent Team 数据模型 — 任务、消息、成员运行时状态。

所有模型仅用于 Team 模式（mode=team）。单 Agent 模式不受影响。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class TeamTaskStatus(str, Enum):
    """Team 任务状态.

    标准流程 (Phase 3 风险分级验收):
      低风险: pending → in_progress → completed (证据校验通过即直通, 免审查)
                                    ↘ in_review (证据缺失/校验失败, fail-safe 转 Lead)
      高风险: pending → in_progress → in_review → 独立 Verifier 验收子任务
                                    │              → PASS → approved (终态)
                                    │              → FAIL → revision_needed → in_progress
                                    ↘ 无 Verifier / VERDICT 解析失败 → Lead task_review 兜底
      通用: in_review → approved / revision_needed (Lead 审查)
            in_progress → failed / cancelled (终态)
    crash 恢复: in_progress → interrupted → 原成员恢复 or 回池 PENDING.

    A2A 映射: submitted=pending, working=in_progress, completed=completed,
              failed=failed, canceled=cancelled.
    """

    PENDING = "pending"               # 等待依赖完成或待分配 (A2A: submitted)
    IN_PROGRESS = "in_progress"       # member 正在执行 (A2A: working)
    IN_REVIEW = "in_review"           # 成员已提交, 等待 Lead 审查
    APPROVED = "approved"             # Lead 审查通过 (终态)
    REVISION_NEEDED = "revision_needed"  # Lead 要求修改, 成员拿回继续
    COMPLETED = "completed"           # 已完成 (终态, 兼容旧行为)
    FAILED = "failed"                 # 执行失败 (终态)
    CANCELLED = "cancelled"           # 已取消 (终态)
    INTERRUPTED = "interrupted"       # 成员中断 (crash 恢复), 保留 assigned_agent + checkpoint

    @property
    def is_terminal(self) -> bool:
        """返回 True 表示任务已到达终态 (不可再流转)."""
        return self in {
            TeamTaskStatus.APPROVED,
            TeamTaskStatus.COMPLETED,
            TeamTaskStatus.FAILED,
            TeamTaskStatus.CANCELLED,
        }

    @property
    def is_success(self) -> bool:
        """返回 True 表示任务以成功终态结束 (用于依赖检查)."""
        return self in {
            TeamTaskStatus.APPROVED,
            TeamTaskStatus.COMPLETED,
        }


class TaskSpec(BaseModel):
    """结构化任务规格 (Phase 2 任务协议 JSON 化).

    全部字段可选/默认空 — 轻任务只填 goal 也合法。
    Lead 委派时由各工具参数组装, member 侧通过 render() 渲染为可读文本。
    """

    background: str = ""                     # 背景 (为什么做)
    goal: str = ""                           # 目标 (交付什么)
    description: str = ""                    # 详细描述
    constraints: list[str] = Field(default_factory=list)   # 约束/注意事项
    format: str = ""                         # 输出格式要求
    acceptance_criteria: list[str] = Field(default_factory=list)  # 验收标准

    def is_empty(self) -> bool:
        """所有字段均为空时视为无结构化 spec."""
        return (
            not self.background and not self.goal and not self.description
            and not self.constraints and not self.format
            and not self.acceptance_criteria
        )

    def render(self) -> str:
        """渲染为 member 可读文本, 供注入任务描述/prompt 使用."""
        sections: list[str] = []
        if self.background:
            sections.append(f"[背景]\n{self.background}")
        if self.goal:
            sections.append(f"[目标]\n{self.goal}")
        if self.description:
            sections.append(f"[描述]\n{self.description}")
        if self.constraints:
            sections.append("[约束]\n" + "\n".join(f"- {c}" for c in self.constraints))
        if self.format:
            sections.append(f"[输出格式]\n{self.format}")
        if self.acceptance_criteria:
            sections.append(
                "[验收标准]\n" + "\n".join(f"- {c}" for c in self.acceptance_criteria)
            )
        return "\n\n".join(sections)


class SkillFeedbackItem(BaseModel):
    """试验性技能使用上报 (Phase 5 Skill 自进化)."""

    name: str                                # 技能名
    success: bool = True                     # 本次使用是否成功


class TaskResult(BaseModel):
    """member 完成输出的结构化结果 (Phase 2 任务协议 JSON 化).

    uncertainty 仅存储展示, 不做任何判断逻辑 (Phase 3 决策: 不参与直通判断)。
    轻任务允许只填 output。
    """

    status: str = ""                         # 完成状态描述 (与任务状态流转解耦)
    output: str = ""                         # 成果总结
    evidence: list[str] = Field(default_factory=list)  # 证据 (文件路径/命令/链接)
    uncertainty: Literal["low", "medium", "high"] = "low"  # 自评不确定性 (仅展示)
    failure_reason: str = ""                 # 失败原因 (status=failed 时)
    # 试验性技能使用上报 (Phase 5; 可选, 历史 JSON 无此字段正常加载)
    skill_feedback: list[SkillFeedbackItem] = Field(default_factory=list)


# ── 风险分级 (Phase 3) ──
# 写操作类信号关键词 — 任务文本命中即推断为 high 风险 (强制验收).
# 模块级常量便于维护; 误判方向偏向 high (多验收), 属安全方向.
RISK_HIGH_KEYWORDS: tuple[str, ...] = (
    # 中文写操作信号
    "写文件", "写入", "修改", "删除", "部署", "执行命令", "运行命令",
    "实现", "开发", "编码", "重构", "迁移", "安装",
    "创建", "新建",  # 创建文件/资源同属写操作 (E2E: "创建 hello.html" 曾误判 low)
    # 英文写操作信号
    "write", "modify", "delete", "deploy", "execute",
    "implement", "refactor", "migrate", "install", "commit",
    "create", "update",
)


def infer_task_risk(task: "TeamTask", has_downstream: bool = False) -> Literal["low", "high"]:
    """程序推断任务风险等级 — Lead 未显式指定时的默认分级 (纯函数).

    high: 标题/描述/spec 命中写操作关键词 | 有下游依赖它的任务
          | acceptance_criteria 非空
    low:  其他 (只读/探索/查询类)
    """
    if has_downstream:
        return "high"
    spec = task.spec
    if spec is not None and spec.acceptance_criteria:
        return "high"
    parts = [task.title or "", task.description or ""]
    if spec is not None:
        parts.extend([spec.goal, spec.background, spec.description, *spec.constraints])
    text = " ".join(parts).lower()
    for kw in RISK_HIGH_KEYWORDS:
        if kw in text:
            return "high"
    return "low"


class TeamTask(BaseModel):
    """Team 任务板上的一个任务。

    任务由 Lead Agent（或用户）创建，分配给 member Agent 执行。
    支持依赖关系：dependencies 中的任务必须先完成。
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    project_id: str
    title: str
    description: str = ""
    status: TeamTaskStatus = TeamTaskStatus.PENDING
    assigned_agent: str | None = None       # 被分配的 member agent name
    dependencies: list[str] = Field(default_factory=list)  # 依赖的任务 ID 列表
    priority: str = "medium"                # "low" | "medium" | "high" | "critical"
    output: str = ""                        # 执行结果文本 (旧协议, result 为空时的回退)
    spec: TaskSpec | None = None            # 结构化任务规格 (Phase 2; 历史任务无此字段)
    result: TaskResult | None = None        # 结构化完成结果 (Phase 2; 历史任务无此字段)
    error: str | None = None                # 失败原因
    retry_count: int = 0                    # 已重试次数 (crash 恢复)
    max_retries: int = 3                    # 最大重试次数 (crash 恢复)
    revision_count: int = 0                 # Review 修改轮次
    review_feedback: str = ""               # Lead 审查反馈 (REVISION_NEEDED 时写入)
    origin: str = "team"                    # "team"=团队运行产生 | "user"=用户手工创建
    risk: Literal["low", "high"] | None = None  # 风险分级 (Phase 3; None=未分级/历史任务)
    risk_locked: bool = False               # Lead 显式指定 → True, 程序推断不再改动
    verifies_task_id: str | None = None     # 验收子任务 → 被验收的原任务 ID (Phase 3)
    created_at: str = ""
    updated_at: str = ""

    def model_post_init(self, __context: Any) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def effective_output(self) -> str:
        """下游消费产出: 优先结构化 result.output, 回退旧 output 字段 (双路径兼容)."""
        if self.result is not None and self.result.output:
            return self.result.output
        return self.output

    def effective_failure_reason(self) -> str:
        """下游消费失败原因: 优先 result.failure_reason, 回退 error 字段."""
        if self.result is not None and self.result.failure_reason:
            return self.result.failure_reason
        return self.error or ""


class TeamMessageType(str, Enum):
    """Team 消息类型 — 包含  结构化协议消息."""

    TEXT = "text"                              # 普通文本消息
    BROADCAST = "broadcast"                    # 广播消息
    LIFECYCLE = "lifecycle"                    # Agent 上下线通知
    # ──  结构化协议消息 ──
    SHUTDOWN_REQUEST = "shutdown_request"      # Lead → Teammate: 请求关闭
    SHUTDOWN_RESPONSE = "shutdown_response"    # Teammate → Lead: 确认/拒绝关闭
    PLAN_APPROVAL_REQUEST = "plan_approval_request"   # Teammate → Lead: 请求审批
    PLAN_APPROVAL_RESPONSE = "plan_approval_response" # Lead → Teammate: 审批结果


class TeamMessage(BaseModel):
    """Team 内 Agent 间消息 — 支持  结构化协议."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    from_agent: str
    to_agent: str | None = None        # None = 广播给所有人
    msg_type: TeamMessageType = TeamMessageType.TEXT
    content: str
    task_id: str | None = None         # 关联的任务 ID
    request_id: str | None = None      #  关联请求和响应 (shutdown/plan approval)
    #  协议响应的结构化结果 (shutdown_response / plan_approval_response):
    # 发送方显式写入, 接收方优先读此字段, 避免对 content 做子串匹配误判
    # (如 Lead 拒绝文案 "not approved yet" 含 "approved").
    approved: bool | None = None
    created_at: str = ""

    def model_post_init(self, __context: Any) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()


class TeammateStatus(str, Enum):
    """Teammate Agent 生命周期状态

    SPAWNING → WORKING ↔ IDLE → SHUTTING_DOWN → SHUTDOWN.
    """

    SPAWNING = "spawning"            # 正在初始化
    WORKING = "working"              # 正在执行任务
    IDLE = "idle"                    # 空闲，等待唤醒 ( 可自主认领)
    SHUTTING_DOWN = "shutting_down"  # 正在优雅关闭 ( shutdown handshake)
    SHUTDOWN = "shutdown"            # 已关闭 (终态)
    FAILED = "failed"                # 异常终止


class TeamMemberRuntime(BaseModel):
    """Team 成员运行时状态."""

    agent_name: str
    role: Literal["lead", "member"] = "member"
    status: TeammateStatus = TeammateStatus.SPAWNING
    current_task_id: str | None = None     # 当前正在执行的任务
    completed_tasks: int = 0
    failed_tasks: int = 0
    last_error: str | None = None


class RequestStatus(str, Enum):
    """协议请求状态 — FSM: pending → approved / rejected.

    用于关机协议和计划审批协议的请求追踪。
    """

    PENDING = "pending"      # 等待审批
    APPROVED = "approved"    # 已批准
    REJECTED = "rejected"    # 已拒绝

