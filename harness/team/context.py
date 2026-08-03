"""TeamContext — Team 运行时上下文数据类。

提供:
- 项目元数据
- 成员列表摘要 (运行时状态)
- 团队能力矩阵 (从 agent-card.json 加载)
- 任务板摘要
- 消息历史摘要

用于注入到 Lead Agent / Member Agent 的 system prompt 中。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from harness.team.models import TeamMemberRuntime

logger = logging.getLogger(__name__)


@dataclass
class TeamContext:
    """Team 运行时上下文 — 在 Project Lead 和 Member Agent 间共享."""

    project_id: str
    project_name: str = ""
    project_description: str = ""
    user_id: str = "default"
    lead_name: str = ""  # Lead Agent 的名称 (用于消息路由)

    # 成员列表（运行时状态）
    members: list[TeamMemberRuntime] = field(default_factory=list)

    # ── 团队能力缓存 (由 Orchestrator 注入) ──
    _team_capabilities_xml: str = ""  # 预格式化的 XML 片段

    # ── 团队记忆缓存 ──
    _team_memory_xml: str = ""  # 预格式化的 <team_memory> XML

    # ------------------------------------------------------------------
    # 成员摘要 (运行时状态)
    # ------------------------------------------------------------------

    def get_member_summary(self) -> str:
        """生成成员摘要文本 (运行时状态), 用于注入 system prompt."""
        if not self.members:
            return "(无成员)"

        lines: list[str] = []
        for m in self.members:
            status_icon = {
                "idle": "🟢",
                "spawning": "🟡",
                "working": "🔵",
                "shutting_down": "🟠",
                "shutdown": "⚫",
                "failed": "❌",
            }.get(m.status, "⚪")
            task_info = f" (当前任务: {m.current_task_id})" if m.current_task_id else ""
            lines.append(
                f"- {status_icon} **{m.agent_name}** ({m.role}) — {m.status}{task_info}"
            )
        return "\n".join(lines)

    def get_project_context_xml(self) -> str:
        """生成项目上下文 XML (含成员运行时状态)."""
        return f"""<project_context>
<project_name>{self.project_name}</project_name>
<project_description>{self.project_description}</project_description>
<project_id>{self.project_id}</project_id>
<team_members>
{self.get_member_summary()}
</team_members>
</project_context>"""

    # ------------------------------------------------------------------
    # 团队能力矩阵 (agent cards)
    # ------------------------------------------------------------------

    def set_team_capabilities(self, cards: dict[str, Any]) -> None:
        """设置团队能力矩阵并预格式化 XML.

        由 Orchestrator.initialize() 在生成 agent cards 后调用.
        """
        from harness.team.agent_card import format_cards_for_prompt
        if cards:
            self._team_capabilities_xml = (
                "<team_capabilities>\n"
                + format_cards_for_prompt(cards)
                + "\n</team_capabilities>"
            )
        else:
            self._team_capabilities_xml = ""

    def get_team_capabilities_xml(self) -> str:
        """返回预格式化的团队能力 XML 片段.

        包含每个 member 的工具、技能、描述, 用于 Lead 做任务分配决策.
        """
        return self._team_capabilities_xml

    # ------------------------------------------------------------------
    # 协作规则
    # ------------------------------------------------------------------

    def get_team_collaboration_rules(self) -> str:
        """返回 Team 协作规则文本."""
        return """<team_collaboration_rules>
**角色分工:**
- **Project Lead** 负责: 理解用户目标 → 拆解任务 → 分配 Member → 汇总输出
- **Member Agent** 负责: 接收任务 → 使用自己的 SOUL + 工具执行 → 更新任务状态 → 报告结果

**任务板使用规则:**
1. Lead 使用 task_create 创建任务，明确指定标题、描述和分配对象
2. Member 接收任务后立即使用 task_update 将状态改为 in_progress
3. Member 完成后使用 task_update 将状态改为 completed 并附上结果
4. 如执行失败，使用 task_update 将状态改为 failed 并说明原因

**通信规则:**
1. Member 遇到需求不清、工具失败或依赖阻塞时，使用 send_message 向 Lead 提问或报告
2. Lead 可使用 send_message 向特定 Member 发送私聊指令，或使用 broadcast 向全体 Member 发送通知
3. Member 之间可使用 send_message 进行领域咨询 (如向具备特定工具的 Member 请求技术帮助)
4. Member 提交高风险操作计划时，使用 request_plan_approval 向 Lead 请求审批
5. Lead 收到 plan_approval_request 后，使用 approve_plan 回复审批结果
6. 禁止在消息中传递大段代码 — 代码应写入文件并通过任务板引用

**约束:**
1. Member Agent 不能委派任务给成员 Agent，但是可以创建子 Agent 来进行并行完成
2. 每个任务同一时间只能由一个 Member 执行
3. 依赖未完成的任务不会被分配给 Member
4. 单个任务最多重试 3 次，仍失败则标记为 failed 等待 Lead 决策
</team_collaboration_rules>"""

    # ------------------------------------------------------------------
    # 团队记忆 (L3)
    # ------------------------------------------------------------------

    def set_team_memory_xml(self, xml: str) -> None:
        """Set the pre-formatted team memory XML for prompt injection.

        Called by Orchestrator after loading team memory from storage.
        """
        self._team_memory_xml = xml

    def get_team_memory_xml(self) -> str:
        """Return the pre-formatted ``<team_memory>`` XML block.

        Returns an empty string if no team memory has been loaded.
        """
        return self._team_memory_xml
