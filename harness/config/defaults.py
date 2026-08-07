"""系统硬编码默认值 (L0) + 不可覆盖的配置项.

三层配置:
  L0: SYSTEM_DEFAULTS     — 系统硬编码默认
  L1: 用户全局 config.yaml — 基础设施覆盖 (sandbox, checkpointer, langfuse, ...)
  L2: Per-Agent config.yaml — 运行时覆盖 (tools, memory, features, ...)

合并后 HARDCODED_OVERRIDES 强制覆盖所有层级;
模型字段 (model/api_key/base_url/辅助模型) 由 ConfigLoader 用 L0 插值结果强制覆盖,
即只能来自服务器环境变量 (harness/.env), 用户 YAML 中的同名键无效.
"""

SYSTEM_DEFAULTS: dict = {
    # ── 工具 ──
    "tool_groups": ["search", "files", "files_readonly", "mcp"],

    # ── 模型 (服务器统一提供 — 由 harness/.env 或进程环境变量注入, 用户不可覆盖) ──
    "model": "${DEFAULT_MODEL:-gpt-4o}",
    "api_key": "${OPENAI_API_KEY:-}",
    "base_url": "${OPENAI_BASE_URL:-https://api.openai.com/v1}",
    "temperature": 0.3,
    "max_tokens": 4096,
    # 辅助模型 — 空字符串表示回退到主模型
    "summary_model": "${SUMMARY_MODEL:-}",
    "title_model": "${TITLE_MODEL:-}",
    "memory_model": "${MEMORY_MODEL:-}",

    # ── 记忆 ──
    "memory": {
        "max_facts": 10,
        "ttl_days": 90,
        "injection_enabled": True,
        "max_injection_tokens": 500,
        "debounce_seconds": 120,
        "fact_confidence_threshold": 0.7,
    },

    # ── 功能开关 ──
    "summarization": {"enabled": True, "trigger_tokens": 20000, "keep_messages": 10},
    "title": {"enabled": True},
    "subagents": {"timeout_seconds": 900, "max_concurrent": 3},
    "guardrail": {"enabled": True},

    # ── 限制 ──
    "limits": {"max_turns": 50, "timeout_seconds": 900},

    # ── Team ──
    "team": {"can_delegate": True, "memory_scope": "agent"},

    # ── 任务记忆 ──
    "task_memory": {
        "enabled": True,
        "max_related_tasks": 3,
        "max_tokens_per_task": 80,
    },

    # ── 团队记忆 ──
    "team_memory": {
        "enabled": True,
        "max_best_practices": 20,
        "max_pitfalls": 20,
        "max_recent_runs": 5,
    },

    # ── 沙箱 (基础设施, L1 覆盖) ──
    "sandbox": {
        "server_url": "http://localhost:8080",
        "image": "python:3.12",
        "resource_cpu": "1",
        "resource_memory": "2Gi",
        "timeout_minutes": 30,
    },

    # ── 持久化 (基础设施, L1 覆盖) ──
    "checkpointer": {"backend": "sqlite", "sqlite_dir": ""},
    "database": {"backend": "sqlite", "sqlite_dir": ""},

    # ── Langfuse (基础设施, L1 覆盖) ──
    "langfuse": {
        "enabled": "${LANGFUSE_ENABLED:-true}",
        "host": "${LANGFUSE_HOST:-https://cloud.langfuse.com}",
        "public_key": "${LANGFUSE_PUBLIC_KEY:-}",
        "secret_key": "${LANGFUSE_SECRET_KEY:-}",
    },

    # ── 上传 ──
    "uploads": {"max_files": 10, "max_file_size": 52428800},
}

# merge 后强制覆盖 — 用户无法在 config.yaml 中关闭
HARDCODED_OVERRIDES: dict = {
    "loop_detection": {"enabled": True},
    "worktree": {"enabled": True},
}
