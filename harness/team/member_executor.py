"""MemberAgentExecutor — 封装 SubagentExecutor，注入 Team 上下文。

每个 Team Member 使用此 executor 执行被分配的任务:
- 使用自己的 SOUL.md + config.yaml（独立人格、工具、记忆）
- 注入 Team 上下文（项目信息、成员列表、任务板、消息）
- 注入任务上下文（当前任务 instruction + 依赖结果摘要）
- 使用精简 SubAgent middleware 链
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import BaseTool

from harness.agents.subagent_executor import SubagentExecutor
from harness.config.agents_config import load_agent_config, load_agent_soul
from harness.models import SubAgentConfig, SubAgentResult, HarnessState
from harness.team.context import TeamContext
from harness.team.models import TeamTask

logger = logging.getLogger(__name__)


class MemberAgentExecutor:
    """Team Member 执行器 — 封装 SubagentExecutor 并注入 Team 上下文。

    与普通 SubAgent 的区别:
    - system prompt = 自定义 SOUL.md + Team 上下文 + 任务上下文
    - 工具集 = 自定义工具组 + Team 工具（send_message, task_update, task_list）
    - 记忆 = per-agent 独立记忆文件
    """

    def __init__(
        self,
        agent_name: str,
        llm: BaseChatModel,
        tools: list[BaseTool],
        team_context: TeamContext,
        parent_state: HarnessState | None = None,
        *,
        skill_storage: Any | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.llm = llm
        self._tools = tools
        self._team_context = team_context
        self._parent_state = parent_state or {}
        self._skill_storage = skill_storage

        # 加载 Agent 配置和 SOUL
        user_id = team_context.user_id
        self._agent_config = load_agent_config(agent_name, user_id=user_id)
        self._agent_soul = load_agent_soul(agent_name, user_id=user_id)

    # ------------------------------------------------------------------
    # System Prompt 构建
    # ------------------------------------------------------------------

    def _build_system_prompt(self, task: TeamTask | None = None) -> str:
        """构建包含 Team 上下文 + 任务上下文的完整 system prompt."""
        parts: list[str] = []

        # 1. Agent SOUL（自定义人格）
        if self._agent_soul:
            parts.append(self._agent_soul)
        elif self._agent_config and self._agent_config.description:
            parts.append(
                f"你是 {self._agent_config.display_name}，"
                f"专注于 {self._agent_config.description}。"
            )

        # 2. 项目上下文
        parts.append(self._team_context.get_project_context_xml())

        # 3. 协作规则
        parts.append(self._team_context.get_team_collaboration_rules())

        # 4. 任务上下文
        if task is not None:
            parts.append(self._build_task_context(task))

        return "\n\n".join(parts)

    @staticmethod
    def _build_task_context(task: TeamTask) -> str:
        """构建当前任务的上下文片段."""
        deps_str = ", ".join(task.dependencies) if task.dependencies else "无"
        return f"""<current_task>
<task_id>{task.id}</task_id>
<title>{task.title}</title>
<description>{task.description}</description>
<dependencies>{deps_str}</dependencies>
<priority>{task.priority}</priority>
</current_task>

<instruction>
请完成上述任务。完成后使用 task_update 更新任务状态为 reviewing 并附上结果。
如果遇到问题，使用 send_message 向 Lead 报告。
</instruction>"""

    # ------------------------------------------------------------------
    # 子 Agent 配置构建
    # ------------------------------------------------------------------

    def _build_subagent_config(self) -> SubAgentConfig:
        """从 AgentConfig 构建 SubAgentConfig."""
        if self._agent_config:
            return SubAgentConfig(
                name=self.agent_name,
                display_name=self._agent_config.display_name,
                description=self._agent_config.description,
                system_prompt=self._agent_soul or self._agent_config.description,
                model=self._agent_config.model,
                tools=self._agent_config.tool_groups if self._agent_config.tool_groups else None,
                skills=self._agent_config.skills,
                max_turns=self._agent_config.max_turns,
                timeout_seconds=self._agent_config.timeout_seconds,
                isolation=self._agent_config.isolation,
            )
        # 默认配置
        return SubAgentConfig(
            name=self.agent_name,
            display_name=self.agent_name,
            description="",
            system_prompt=self._agent_soul or "",
            model="inherit",
            max_turns=50,
            timeout_seconds=900,
        )

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    async def execute(
        self,
        instruction: str,
        task: TeamTask | None = None,
        context: dict[str, Any] | None = None,
    ) -> SubAgentResult:
        """执行 Team 成员任务。

        Args:
            instruction: 任务指令（来自 Lead Agent 的 delegate_to_member）
            task: 关联的 TeamTask（用于注入任务上下文）
            context: 额外的上下文信息

        Returns:
            SubAgentResult 执行结果
        """
        import asyncio

        subagent_config = self._build_subagent_config()

        # 构建完整的 system prompt（SOUL + Team + Task 上下文）
        full_prompt = self._build_system_prompt(task)

        # 构建初始消息
        messages: list[Any] = []
        if full_prompt:
            messages.append(SystemMessage(content=full_prompt))
        messages.append(HumanMessage(content=instruction))

        # 使用 SubagentExecutor 执行
        executor = SubagentExecutor(
            config=subagent_config,
            llm=self.llm,
            tools=self._tools,
            parent_state=self._parent_state,
            skill_storage=self._skill_storage,
        )

        # 注入初始消息（绕过 _build_initial_state）
        # 在 SubagentExecutor 的 _aexecute 中调用 _build_initial_state
        # 这里我们直接调用 execute，但因为 system prompt 不同，
        # 需要覆盖 _build_initial_state 的行为
        #
        # 方案: 将 system prompt 设为 config.system_prompt,
        # execute() 内部会在 _build_initial_state 时使用它
        executor.config.system_prompt = full_prompt

        # 执行（使用 asyncio.to_thread 避免阻塞）
        result = await asyncio.to_thread(executor.execute, instruction)

        logger.info(
            "Member '%s' executed task: status=%s iterations=%d",
            self.agent_name,
            result.status,
            result.iterations,
        )
        return result
