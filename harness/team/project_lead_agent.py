"""ProjectLeadAgent — Team 模式的 Lead Agent。

与普通 LeadAgent 的区别:
- system prompt 包含: 项目上下文、成员列表、任务板摘要、协作规则
- 工具包含: Team 工具（delegate_to_member, task_create 等）
- 排除: task、create_subagent（Team 模式用 delegate_to_member 代替）
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool

from harness.agents.lead_agent import LeadAgent
from harness.team.context import TeamContext
from harness.team.tools import create_team_tools
from harness.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ProjectLeadAgent:
    """Team 模式 Lead Agent — 项目级编排。

    职责: 理解用户目标 → 拆解任务 → 分配 Member → 监控进度 → 汇总输出
    """

    def __init__(
        self,
        tool_registry: ToolRegistry,
        team_context: TeamContext,
        subagent_manager: Any | None = None,
        task_store: Any | None = None,
        message_bus: Any | None = None,
        config_manager: Any | None = None,
        skill_storage: Any | None = None,
        teammates: dict | None = None,
    ) -> None:
        self._tool_registry = tool_registry
        self._team_context = team_context
        self._subagent_manager = subagent_manager
        self._task_store = task_store
        self._message_bus = message_bus
        self._config_manager = config_manager
        self._skill_storage = skill_storage
        self._teammates = teammates

        # 复用 LeadAgent 的基础功能（工具注册表查询等）
        self._lead_agent = LeadAgent(
            tool_registry=tool_registry,
            subagent_manager=subagent_manager,
            config_manager=config_manager,
            skill_storage=skill_storage,
        )

    # ------------------------------------------------------------------
    # System Prompt
    # ------------------------------------------------------------------

    def get_system_prompt(self) -> str:
        """构建 Team 模式 system prompt."""
        parts: list[str] = []

        # 角色
        parts.append(self._build_role_section())

        # 项目上下文
        parts.append(self._team_context.get_project_context_xml())

        # 协作规则
        parts.append(self._team_context.get_team_collaboration_rules())

        # 工具使用指南
        parts.append(self._build_tool_guidance_section())

        # 工作目录
        parts.append(self._build_workspace_section())

        return "\n\n".join(parts)

    @staticmethod
    def _build_role_section() -> str:
        return """<role>
你是 Project Lead Agent，负责协调 Team 中的多个持久化 Teammate Agent 完成项目目标。

**你的核心职责:**
1. **理解目标**: 分析用户需求，明确成功标准
2. **拆解任务**: 将大目标拆分为可并行/顺序执行的小任务
3. **分配成员**: 根据每个 Member 的专业领域和当前状态分配任务
4. **监控进度**: 通过 task_list 和 list_teammates 跟踪进度
5. **动态调整**: 看到中间结果后创建新任务、调整依赖、重新分配
6. **汇总输出**: 汇总所有子任务结果，生成最终回复

**原则:**
- 任务拆分要细粒度、可验证，每个任务的输出必须明确
- 优先并行分配独立任务，充分利用团队的并发能力
- 遇到需求不明确时，先向用户澄清再拆分任务
- 使用 list_teammates 了解谁空闲、谁忙碌后再分配
- 每个 Teammate 是持久化运行的，任务完成后回到 IDLE，可继续分配新任务
</role>"""

    @staticmethod
    def _build_tool_guidance_section() -> str:
        return """<team_tool_guidance>
**任务板工具:**
- `task_create`: 创建新任务。任务描述必须包含5要素（目标/背景/范围/约束/格式）
- `task_list`: 查看当前任务板状态，了解进度
- `task_update`: 更新任务状态（完成/失败/取消）

**委派工具:**
- `delegate_to_member`: 将任务分配给指定 Member 执行

**团队管理工具:**
- `list_teammates`: 查看所有 teammate 的当前状态（角色、状态、当前任务）

**通信工具:**
- `broadcast`: 向全体 Member 发送通知（如：全体注意，XX任务优先级提升）
- `send_message`: 向特定 Member 发送私聊消息（如：补充说明、追问细节）
- `read_inbox`: 读取自己的收件箱，查看其他 Agent 发来的消息

**典型工作流:**
1. 分析用户目标 → 使用 task_create 创建 1-N 个任务
2. 对每个任务 → 使用 delegate_to_member 分配给合适的 Member
3. 使用 list_teammates 查看团队状态
4. 等待执行完成 → 使用 task_list 查看进度
5. 使用 read_inbox 检查是否有 Member 发来的消息
6. 全部完成后 → 汇总结果生成最终回复
</team_tool_guidance>"""

    @staticmethod
    def _build_workspace_section() -> str:
        return """<working_directory>
- User uploads: `/mnt/user-data/uploads`
- User workspace: `/mnt/user-data/workspace`
- Output files: `/mnt/user-data/outputs`

所有文件操作使用 workspace-relative 路径。
</working_directory>"""

    # ------------------------------------------------------------------
    # 工具构建
    # ------------------------------------------------------------------

    def build_tools(self) -> list[BaseTool]:
        """构建 Team Lead 工具集。

        包含:
        - ask_clarification（始终可用）
        - Team 工具（delegate_to_member, task_create 等）
        - Lead Agent 的基础工具（从 config.yaml 配置）
        - 排除: task, create_subagent（Team 模式不适用）
        """
        tools: list[BaseTool] = []

        # 1. ask_clarification（始终需要）
        from harness.tools.builtins.lead_tools import ask_clarification_tool
        tools.append(ask_clarification_tool())

        # 2. Team 专用工具
        team_tools = create_team_tools(
            task_store=self._task_store,
            message_bus=self._message_bus,
            subagent_manager=self._subagent_manager,
            teammates=self._teammates,
            role="lead",
        )
        tools.extend(team_tools)

        # 3. Lead Agent 的基础工具（排除 task 和 create_subagent）
        lead_base = self._lead_agent.build_tools()
        excluded_in_team = {"task", "create_subagent"}
        for t in lead_base:
            if t.name not in excluded_in_team:
                # 避免重复添加（如 ask_clarification）
                if not any(existing.name == t.name for existing in tools):
                    tools.append(t)

        logger.info(
            "ProjectLeadAgent: built %d tools for team mode (excluded: %s)",
            len(tools), excluded_in_team,
        )
        return tools

    # ------------------------------------------------------------------
    # 上下文注入
    # ------------------------------------------------------------------

    def get_team_context_for_middleware(self) -> str:
        """返回用于 Middleware 注入的 Team 上下文 XML."""
        return self._team_context.get_project_context_xml()
