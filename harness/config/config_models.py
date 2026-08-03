"""配置数据模型 — UserGlobalConfig, AgentRuntimeConfig, EffectiveConfig, ExtensionsAgentConfig."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# L1: 用户全局 config.yaml
# ---------------------------------------------------------------------------

class SandboxGlobalConfig(BaseModel):
    server_url: str = "http://localhost:8080"
    image: str = "python:3.12"
    resource_cpu: str = "1"
    resource_memory: str = "2Gi"
    timeout_minutes: int = 30


class CheckpointerGlobalConfig(BaseModel):
    backend: str = "sqlite"
    sqlite_dir: str = ""


class DatabaseGlobalConfig(BaseModel):
    backend: str = "sqlite"
    sqlite_dir: str = ""


class LangfuseGlobalConfig(BaseModel):
    enabled: bool = True
    host: str = "https://cloud.langfuse.com"
    public_key: str = ""
    secret_key: str = ""


class SummarizationGlobalConfig(BaseModel):
    enabled: bool = True
    trigger_tokens: int = 20000
    keep_messages: int = 10


class TitleGlobalConfig(BaseModel):
    enabled: bool = True


class MemoryGlobalConfig(BaseModel):
    debounce_seconds: float = 120.0
    fact_confidence_threshold: float = 0.7


class UserGlobalConfig(BaseModel):
    """用户全局配置 (~/.multiagent-studio/users/{uid}/config.yaml)."""
    sandbox: SandboxGlobalConfig = Field(default_factory=SandboxGlobalConfig)
    checkpointer: CheckpointerGlobalConfig = Field(default_factory=CheckpointerGlobalConfig)
    database: DatabaseGlobalConfig = Field(default_factory=DatabaseGlobalConfig)
    langfuse: LangfuseGlobalConfig = Field(default_factory=LangfuseGlobalConfig)
    summarization: SummarizationGlobalConfig = Field(default_factory=SummarizationGlobalConfig)
    title: TitleGlobalConfig = Field(default_factory=TitleGlobalConfig)
    memory: MemoryGlobalConfig = Field(default_factory=MemoryGlobalConfig)


# ---------------------------------------------------------------------------
# L2: Per-Agent config.yaml
# ---------------------------------------------------------------------------

class AgentMemoryConfig(BaseModel):
    max_facts: int = 10
    injection_enabled: bool = True
    max_injection_tokens: int = 500


class AgentFeaturesConfig(BaseModel):
    summarization: bool = True
    subagent: bool = True
    langfuse: bool = True
    guardrail: bool = False


class AgentLimitsConfig(BaseModel):
    max_turns: int = 50
    timeout_seconds: int = 900


class AgentTeamConfig(BaseModel):
    can_delegate: bool = True
    memory_scope: str = "agent"
    # 成员工作区隔离 (Phase 6): worktree=独立 git worktree, 与其他成员隔离;
    # shared=共享 thread 工作区 (默认, 协作产物互见)
    isolation: str = "shared"


class AgentSubagentsConfig(BaseModel):
    timeout_seconds: int = 900
    max_concurrent: int = 3


class AgentRuntimeConfig(BaseModel):
    """Per-Agent 运行时配置 (~/.multiagent-studio/users/{uid}/agents/{name}/config.yaml)."""
    name: str = ""
    display_name: str = ""
    description: str = ""
    model: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096
    tool_groups: list[str] = Field(default_factory=list)  # 扩展到 L0
    skills: list[str] = Field(default_factory=list)

    memory: AgentMemoryConfig = Field(default_factory=AgentMemoryConfig)
    features: AgentFeaturesConfig = Field(default_factory=AgentFeaturesConfig)
    limits: AgentLimitsConfig = Field(default_factory=AgentLimitsConfig)
    team: AgentTeamConfig = Field(default_factory=AgentTeamConfig)
    subagents: AgentSubagentsConfig = Field(default_factory=AgentSubagentsConfig)

    created_at: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------------------
# Per-Agent extensions_config.yaml
# ---------------------------------------------------------------------------

class ExtensionsAgentConfig(BaseModel):
    """Per-Agent 扩展配置 (MCP server 启用子集)."""
    mcp_servers: dict[str, bool] = Field(default_factory=dict)
    skills: dict[str, bool] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 合并后的运行时配置 (flat dataclass, 方便使用)
# ---------------------------------------------------------------------------

@dataclass
class EffectiveConfig:
    """合并后的运行时配置 — 供所有组件直接读取."""

    # 模型
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096
    # 辅助模型 — 空字符串回退到 model
    summary_model: str = ""
    title_model: str = ""
    memory_model: str = ""

    # 工具
    tool_groups: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)

    # 记忆
    memory_max_facts: int = 10
    memory_ttl_days: int = 90
    memory_injection_enabled: bool = True
    memory_max_injection_tokens: int = 500
    memory_debounce_seconds: float = 120.0
    memory_fact_confidence_threshold: float = 0.7

    # 任务记忆
    task_memory_enabled: bool = True
    task_memory_max_related: int = 3
    task_memory_max_tokens_per_task: int = 80

    # 团队记忆
    team_memory_enabled: bool = True

    # 功能开关
    summarization_enabled: bool = True
    title_enabled: bool = True
    subagent_enabled: bool = True
    langfuse_enabled: bool = True
    loop_detection_enabled: bool = True
    worktree_enabled: bool = True
    guardrail_enabled: bool = False

    # 限制
    max_turns: int = 50
    timeout_seconds: int = 900
    subagent_timeout_seconds: int = 900
    max_concurrent_subagents: int = 3

    # Team
    can_delegate: bool = True
    memory_scope: str = "agent"
    agent_name: str = ""
    agent_display_name: str = ""
    agent_description: str = ""
    agent_soul: str = ""

    # 基础设施
    sandbox_server_url: str = "http://localhost:8080"
    sandbox_image: str = "python:3.12"
    sandbox_resource_cpu: str = "1"
    sandbox_resource_memory: str = "2Gi"
    sandbox_timeout_minutes: int = 30
    checkpointer_backend: str = "sqlite"
    checkpointer_sqlite_dir: str = ""
    database_backend: str = "sqlite"
    database_sqlite_dir: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # MCP / 扩展
    enabled_mcp_servers: dict[str, bool] = field(default_factory=dict)
    enabled_skills: dict[str, bool] = field(default_factory=dict)

    # 原始合并结果 (给需要完整 dict 的中间件)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_merged(cls, merged: dict[str, Any], *, agent_soul: str = "") -> "EffectiveConfig":
        """从合并后的 dict 构造 EffectiveConfig."""
        mem = merged.get("memory", {})
        sandbox = merged.get("sandbox", {})
        ckp = merged.get("checkpointer", {})
        db = merged.get("database", {})
        lf = merged.get("langfuse", {})
        summarization = merged.get("summarization", {})
        title = merged.get("title", {})
        subagents = merged.get("subagents", {})
        limits = merged.get("limits", {})
        team = merged.get("team", {})
        tm = merged.get("task_memory", {})
        tm2 = merged.get("team_memory", {})
        loop = merged.get("loop_detection", {})
        wt = merged.get("worktree", {})
        guardrail = merged.get("guardrail", {})
        features = merged.get("features", {})

        return cls(
            model=merged.get("model", ""),
            api_key=merged.get("api_key", ""),
            base_url=merged.get("base_url", ""),
            temperature=float(merged.get("temperature", 0.3)),
            max_tokens=int(merged.get("max_tokens", 4096)),
            summary_model=merged.get("summary_model", ""),
            title_model=merged.get("title_model", ""),
            memory_model=merged.get("memory_model", ""),
            tool_groups=merged.get("tool_groups", []),
            skills=merged.get("skills", []),
            memory_max_facts=int(mem.get("max_facts", 10)),
            memory_ttl_days=int(mem.get("ttl_days", 90)),
            memory_injection_enabled=bool(mem.get("injection_enabled", True)),
            memory_max_injection_tokens=int(mem.get("max_injection_tokens", 500)),
            memory_debounce_seconds=float(mem.get("debounce_seconds", 120)),
            memory_fact_confidence_threshold=float(mem.get("fact_confidence_threshold", 0.7)),
            task_memory_enabled=bool(tm.get("enabled", True)),
            task_memory_max_related=int(tm.get("max_related_tasks", 3)),
            task_memory_max_tokens_per_task=int(tm.get("max_tokens_per_task", 80)),
            team_memory_enabled=bool(tm2.get("enabled", True)),
            summarization_enabled=bool(summarization.get("enabled", True)),
            title_enabled=bool(title.get("enabled", True)),
            subagent_enabled=bool(features.get("subagent", True)),
            langfuse_enabled=(
                # L0→L1→L2 merge 后的值 (可能已被 _interpolate_env 转为 bool)
                bool(lf.get("enabled", True))
                # 额外安全机制: 如果 env var 明确设为 false, 强制禁用
                and os.environ.get("LANGFUSE_ENABLED", "true").lower() != "false"
            ),
            loop_detection_enabled=bool(loop.get("enabled", True)),
            worktree_enabled=bool(wt.get("enabled", True)),
            guardrail_enabled=bool(guardrail.get("enabled", False)),
            max_turns=int(limits.get("max_turns", 50)),
            timeout_seconds=int(limits.get("timeout_seconds", 900)),
            subagent_timeout_seconds=int(subagents.get("timeout_seconds", 900)),
            max_concurrent_subagents=int(subagents.get("max_concurrent", 3)),
            can_delegate=bool(team.get("can_delegate", True)),
            memory_scope=str(team.get("memory_scope", "agent")),
            agent_name=merged.get("name", ""),
            agent_display_name=merged.get("display_name", ""),
            agent_description=merged.get("description", ""),
            agent_soul=agent_soul,
            sandbox_server_url=sandbox.get("server_url", "http://localhost:8080"),
            sandbox_image=sandbox.get("image", "python:3.12"),
            sandbox_resource_cpu=sandbox.get("resource_cpu", "1"),
            sandbox_resource_memory=sandbox.get("resource_memory", "2Gi"),
            sandbox_timeout_minutes=int(sandbox.get("timeout_minutes", 30)),
            checkpointer_backend=ckp.get("backend", "sqlite"),
            checkpointer_sqlite_dir=ckp.get("sqlite_dir", ""),
            database_backend=db.get("backend", "sqlite"),
            database_sqlite_dir=db.get("sqlite_dir", ""),
            langfuse_host=lf.get("host", "https://cloud.langfuse.com"),
            langfuse_public_key=lf.get("public_key", ""),
            langfuse_secret_key=lf.get("secret_key", ""),
            enabled_mcp_servers=merged.get("_ext_mcp_servers", {}),
            enabled_skills=merged.get("_ext_skills", {}),
            raw=merged,
        )
