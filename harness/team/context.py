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
            return "(no members)"

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
            task_info = f" (current task: {m.current_task_id})" if m.current_task_id else ""
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
**Role split:**
- **Project Lead** is responsible for: understanding the user goal → breaking down tasks → assigning Members → aggregating output
- **Member Agent** is responsible for: receiving tasks → executing with its own SOUL + tools → updating task status → reporting results

**Task board rules:**
1. Lead uses task_create to create tasks, clearly specifying title, description and assignee
2. Member immediately uses task_update to set status to in_progress after receiving a task
3. Member uses task_update to set status to completed with the result attached when done
4. If execution fails, use task_update to set status to failed and explain the reason

**Communication rules:**
1. When a Member hits unclear requirements, tool failures, or blocked dependencies, use send_message to ask or report to the Lead
2. Lead can use send_message to send direct instructions to a specific Member, or broadcast to notify all Members
3. Members can use send_message for domain consultation (e.g. requesting technical help from a Member with specific tools)
4. When a Member submits a high-risk operation plan, use request_plan_approval to ask the Lead for approval
5. After receiving a plan_approval_request, Lead replies with the approval result via approve_plan
6. Do not pass large blocks of code in messages — code should be written to files and referenced via the task board

**Constraints:**
1. A Member Agent cannot delegate tasks to other member Agents, but can spawn sub-Agents for parallel work
2. Each task can only be executed by one Member at a time
3. Tasks with incomplete dependencies will not be assigned to Members
4. A single task retries at most 3 times; if it still fails it is marked failed and awaits Lead's decision
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
