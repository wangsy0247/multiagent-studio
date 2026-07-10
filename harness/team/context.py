"""TeamContext — Team 运行时上下文数据类。

提供:
- 项目元数据
- 成员列表摘要
- 任务板摘要
- 消息历史摘要

用于注入到 Lead Agent / Member Agent 的 system prompt 中。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from harness.team.models import TeamMemberRuntime


@dataclass
class TeamContext:
    """Team 运行时上下文 — 在 Project Lead 和 Member Agent 间共享."""

    project_id: str
    project_name: str = ""
    project_description: str = ""
    thread_id: str = ""
    user_id: str = "default"

    # 成员列表（运行时状态）
    members: list[TeamMemberRuntime] = field(default_factory=list)

    def get_member_summary(self) -> str:
        """生成成员摘要文本，用于注入 system prompt."""
        if not self.members:
            return "(无成员)"

        lines: list[str] = []
        for m in self.members:
            status_icon = {
                "idle": "🟢",
                "busy": "🔵",
                "done": "✅",
                "failed": "❌",
            }.get(m.status, "⚪")
            task_info = f" (当前任务: {m.current_task_id})" if m.current_task_id else ""
            lines.append(
                f"- {status_icon} **{m.agent_name}** ({m.role}) — {m.status}{task_info}"
            )
        return "\n".join(lines)

    def get_project_context_xml(self) -> str:
        """生成项目上下文的 XML 片段."""
        return f"""<project_context>
<project_name>{self.project_name}</project_name>
<project_description>{self.project_description}</project_description>
<project_id>{self.project_id}</project_id>
<team_members>
{self.get_member_summary()}
</team_members>
</project_context>"""

    def get_team_collaboration_rules(self) -> str:
        """返回 Team 协作规则文本."""
        return """<team_collaboration_rules>
**角色分工:**
- **Project Lead** 负责: 理解用户目标 → 拆解任务 → 分配 Member → 审阅结果 → 合并输出
- **Member Agent** 负责: 接收任务 → 使用自己的 SOUL + 工具执行 → 更新任务状态 → 报告结果

**任务板使用规则:**
1. Lead 使用 task_create 创建任务，明确指定标题、描述和分配对象
2. Member 接收任务后立即使用 task_update 将状态改为 in_progress
3. Member 完成后使用 task_update 将状态改为 reviewing 并附上结果摘要
4. Lead 审阅通过后使用 task_update 将状态改为 completed

**通信规则:**
1. Member 遇到需求不清时，使用 send_message 向 Lead 提问
2. Member 遇到阻塞（工具失败、依赖未完成）时，将任务标记为 failed 并说明原因
3. Lead 可使用 broadcast 向全体 Member 发送通知
4. 禁止在消息中传递大段代码 — 代码应写入文件并通过任务板引用

**约束:**
1. Member Agent 不能委派任务给其他 Agent
2. 每个任务同一时间只能由一个 Member 执行
3. 依赖未完成的任务不会被分配给 Member
4. 单个任务最多重试 3 次，仍失败则标记为 failed 等待 Lead 决策
</team_collaboration_rules>"""
