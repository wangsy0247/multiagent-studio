"""ConfigLoader — 三层配置加载 + 合并 (L0 系统默认 → L1 用户全局 → L2 Per-Agent).

支持 ``${VAR}`` 和 ``$VAR`` 环境变量替换 (与 ConfigManager 一致).
"""

from __future__ import annotations

import logging
import os
import re
from copy import deepcopy
from pathlib import Path

import yaml

from harness.config.defaults import HARDCODED_OVERRIDES, SYSTEM_DEFAULTS
from harness.config.config_models import EffectiveConfig

logger = logging.getLogger(__name__)

GLOBAL_CONFIG_FILENAME = "config.yaml"
AGENT_CONFIG_FILENAME = "config.yaml"
AGENT_EXTENSIONS_FILENAME = "extensions_config.yaml"

# 服务器统一管理的模型字段 — merge 完成后用 L0 (env 插值) 的值强制覆盖,
# 用户全局 / agent YAML 中的同名键一律无效.
SERVER_FORCED_KEYS = (
    "model",
    "api_key",
    "base_url",
    "summary_model",
    "title_model",
    "memory_model",
)

# 匹配 $VAR, ${VAR}, ${VAR:-default}, ${VAR-default}
_env_var_re = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}|\$(\w+)")


def _resolve_env(match: re.Match) -> str:
    """替换 ``$VAR`` / ``${VAR}`` / ``${VAR:-default}`` 为环境变量值.

    - ``${VAR:-default}`` → 查找 VAR, 未设置则返回 default
    - ``${VAR}`` 或 ``$VAR`` → 查找 VAR, 未设置则返回 ""
    """
    var_name = match.group(1) or match.group(3)
    default_val = match.group(2)
    if default_val is not None:
        return os.environ.get(var_name, default_val)
    return os.environ.get(var_name, "")


def _interpolate_env(obj):
    """递归替换对象中的 ``$VAR`` / ``${VAR}`` 表达式.

    ``${VAR:-default}`` 会被替换为环境变量值或默认值.
    替换后如果结果是 "true" / "false" 字符串, 自动转换为 Python bool.
    """
    if isinstance(obj, dict):
        return {k: _interpolate_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate_env(v) for v in obj]
    if isinstance(obj, str):
        result: str = _env_var_re.sub(_resolve_env, obj)
        # 将布尔字符串转换为 Python bool
        # 例如 "${LANGFUSE_ENABLED:-true}" → "true" → True
        lower = result.lower()
        if lower == "true":
            return True
        if lower == "false":
            return False
        return result
    return obj


class ConfigLoader:
    """加载并合并三层配置, 返回 EffectiveConfig.

    使用方式::

        effective = ConfigLoader.load_effective(user_id="default", agent_name="coder")
        llm = ChatOpenAI(model=effective.model, temperature=effective.temperature)
    """

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------

    @staticmethod
    def load_effective(
        user_id: str,
        agent_name: str,
        *,
        base_dir: str | Path | None = None,
    ) -> EffectiveConfig:
        """加载三层配置并返回合并后的 EffectiveConfig.

        Args:
            user_id: 用户 ID
            agent_name: Agent 名称
            base_dir: 数据根目录, 默认 ~/.multiagent-studio

        Returns:
            EffectiveConfig: 合并后的运行时配置
        """
        if base_dir is None:
            from harness.config.paths import get_paths
            base_dir = get_paths().base_dir

        base = Path(base_dir)
        user_dir = base / "users" / user_id

        # L0: 系统默认 (应用 env var 替换)
        l0 = _interpolate_env(deepcopy(SYSTEM_DEFAULTS))
        merged = l0

        # L1: 用户全局 config
        user_global = ConfigLoader._load_yaml(user_dir / GLOBAL_CONFIG_FILENAME)
        if user_global:
            merged = ConfigLoader._deep_merge(merged, user_global)

        # L2: Per-Agent config
        agent_config_path = user_dir / "agents" / agent_name / AGENT_CONFIG_FILENAME
        if agent_config_path.exists():
            agent_raw = ConfigLoader._load_yaml(agent_config_path)
            if agent_raw:
                merged = ConfigLoader._merge_agent(merged, agent_raw)
        elif agent_name != "default":
            # 非 default agent 必须存在 config.yaml
            raise ValueError(
                f"Agent '{agent_name}' 不存在。"
                f" 请先通过 API 或前端创建该 Agent, 或使用 default agent。"
                f" 配置文件路径: {agent_config_path}"
            )

        # L2b: Per-Agent extensions
        extensions = ConfigLoader._load_yaml(
            user_dir / "agents" / agent_name / AGENT_EXTENSIONS_FILENAME
        )
        if extensions:
            merged["_ext_mcp_servers"] = extensions.get("mcp_servers", {})
            merged["_ext_skills"] = extensions.get("skills", {})

        # 强制覆盖不可配置项
        merged = ConfigLoader._apply_hardcoded(merged)

        # 模型字段由服务器统一提供 (L0 env 插值), 用户/agent YAML 中的同名键无效
        for key in SERVER_FORCED_KEYS:
            if key in l0:
                merged[key] = l0[key]

        # 加载 SOUL
        agent_soul = ConfigLoader._load_agent_soul(user_dir, agent_name)

        logger.info(
            "ConfigLoader: effective config for '%s/%s' — model=%s tool_groups=%s",
            user_id, agent_name, merged.get("model", "?"), merged.get("tool_groups", []),
        )
        return EffectiveConfig.from_merged(merged, agent_soul=agent_soul)

    # ------------------------------------------------------------------
    # 单层加载
    # ------------------------------------------------------------------

    @staticmethod
    def load_user_global(user_id: str, *, base_dir: str | Path | None = None) -> dict | None:
        """加载用户全局 config.yaml (L1)."""
        if base_dir is None:
            from harness.config.paths import get_paths
            base_dir = get_paths().base_dir
        return ConfigLoader._load_yaml(
            Path(base_dir) / "users" / user_id / GLOBAL_CONFIG_FILENAME
        )

    @staticmethod
    def load_agent_runtime(
        user_id: str, agent_name: str, *, base_dir: str | Path | None = None
    ) -> dict | None:
        """加载 Per-Agent config.yaml (L2)."""
        if base_dir is None:
            from harness.config.paths import get_paths
            base_dir = get_paths().base_dir
        return ConfigLoader._load_yaml(
            Path(base_dir) / "users" / user_id / "agents" / agent_name / AGENT_CONFIG_FILENAME
        )

    @staticmethod
    def load_agent_extensions(
        user_id: str, agent_name: str, *, base_dir: str | Path | None = None
    ) -> dict | None:
        """加载 Per-Agent extensions_config.yaml."""
        if base_dir is None:
            from harness.config.paths import get_paths
            base_dir = get_paths().base_dir
        return ConfigLoader._load_yaml(
            Path(base_dir) / "users" / user_id / "agents" / agent_name / AGENT_EXTENSIONS_FILENAME
        )

    # ------------------------------------------------------------------
    # 合并策略
    # ------------------------------------------------------------------

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        """递归合并 — override 中的值替换 base 中的同名 key.

        对于嵌套 dict: 深度合并.
        对于列表: 完全替换 (tool_groups 除外, 由 _merge_agent 处理).
        """
        result = deepcopy(base)
        for key, val in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(val, dict):
                result[key] = ConfigLoader._deep_merge(result[key], val)
            else:
                result[key] = deepcopy(val)
        return result

    @staticmethod
    def _merge_agent(base: dict, agent: dict) -> dict:
        """L2 Agent 配置合并 — tool_groups 扩展, 其他 section 替换."""
        result = deepcopy(base)

        for key, val in agent.items():
            if key == "tool_groups" and isinstance(val, list):
                # 扩展到 L0 的系统默认 tool_groups
                existing = set(result.get("tool_groups", []))
                for g in val:
                    existing.add(g)
                result["tool_groups"] = list(existing)
            elif key == "skills" and isinstance(val, list):
                existing = set(result.get("skills", []))
                for s in val:
                    existing.add(s)
                result["skills"] = list(existing)
            elif key in result and isinstance(result[key], dict) and isinstance(val, dict):
                result[key] = ConfigLoader._deep_merge(result[key], val)
            else:
                result[key] = deepcopy(val)

        return result

    @staticmethod
    def _apply_hardcoded(merged: dict) -> dict:
        """HARDCODED_OVERRIDES 强制覆盖."""
        return ConfigLoader._deep_merge(merged, deepcopy(HARDCODED_OVERRIDES))

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _load_yaml(path: Path) -> dict | None:
        """加载 YAML 文件 (含 ``${VAR}`` 环境变量替换), 不存在返回 None."""
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                data = _interpolate_env(data)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning("ConfigLoader: failed to load %s: %s", path, exc)
            return None

    @staticmethod
    def _load_agent_soul(user_dir: Path, agent_name: str) -> str:
        """加载 agent SOUL.md."""
        soul_path = user_dir / "agents" / agent_name / "SOUL.md"
        if soul_path.exists():
            return soul_path.read_text(encoding="utf-8").strip()
        return ""


# ---------------------------------------------------------------------------
# 用户初始化: 注册时自动创建全局配置 + default agent
# ---------------------------------------------------------------------------

def create_user_configs(user_id: str, *, base_dir: str | Path | None = None) -> None:
    """新用户注册时调用 — 创建用户全局 config.yaml + default agent.

    幂等: 如果 config 已存在则跳过.
    """
    if base_dir is None:
        from harness.config.paths import get_paths
        base_dir = get_paths().base_dir
    base = Path(base_dir)
    user_dir = base / "users" / user_id
    config_path = user_dir / GLOBAL_CONFIG_FILENAME

    # 如果用户全局 config 已存在, 跳过 (幂等)
    if config_path.exists():
        logger.info("User global config already exists for '%s'", user_id)
    else:
        user_dir.mkdir(parents=True, exist_ok=True)
        _write_user_global_config(config_path)
        logger.info("Created user global config: %s", config_path)

    # 确保 default agent 存在
    from harness.config.agents_config import create_default_agent
    create_default_agent(user_id)


def _write_user_global_config(path: Path) -> None:
    """生成用户全局 config.yaml (L1).

    模型 API 由服务器统一提供 (harness/.env), 用户只保留功能开关与记忆策略.
    """
    content = format_user_global_config_yaml({
        "summarization": {"enabled": True},
        "title": {"enabled": True},
        "memory": {
            "debounce_seconds": 120,
            "max_injection_tokens": 500,
            "fact_confidence_threshold": 0.7,
        },
    })
    path.write_text(content, encoding="utf-8")


def format_user_global_config_yaml(config: dict, extra: dict | None = None) -> str:
    """将 L1 用户全局配置 dict 格式化为层级化 YAML 字符串.

    Args:
        config: 用户可配置项 (summarization / title / memory / config_version).
        extra: 需要原样保留的其他字段 (sandbox/checkpointer/database/langfuse 等
            基础设施配置), 以 yaml.safe_dump 追加到文件末尾, 避免整体重写时丢失.
    """
    import yaml as _yaml

    def _get(key: str, default=None):
        return config.get(key, default)

    sections: list[str] = []
    header = "# ════════════════════════════════════════════════════════════════"
    sections.append(header)
    sections.append("# 用户全局配置 (L1) — 通过前端「设置」页面修改")
    sections.append("# 模型 API 由服务器统一配置 (harness/.env), 此处不出现 api_key/model")
    sections.append(header)
    sections.append(f"config_version: {_get('config_version', 1)}")
    sections.append("")

    # ── 功能开关 ──
    sections.append("# ── 功能开关 ──")
    summ_cfg = _get("summarization", {})
    sections.append(f"summarization:")
    sections.append(f"  enabled: {str(summ_cfg.get('enabled', True)).lower()}")
    sections.append("")
    title_cfg = _get("title", {})
    sections.append(f"title:")
    sections.append(f"  enabled: {str(title_cfg.get('enabled', True)).lower()}")
    sections.append("")

    # ── 记忆 ──
    sections.append("# ── 记忆 ──")
    mem_cfg = _get("memory", {})
    sections.append(f"memory:")
    sections.append(f"  max_facts: {mem_cfg.get('max_facts', 100)}")
    sections.append(f"  ttl_days: {mem_cfg.get('ttl_days', 90)}")
    sections.append(f"  debounce_seconds: {mem_cfg.get('debounce_seconds', 120)}")
    sections.append(f"  max_injection_tokens: {mem_cfg.get('max_injection_tokens', 500)}")
    sections.append(f"  fact_confidence_threshold: {mem_cfg.get('fact_confidence_threshold', 0.7)}")
    sections.append("")

    # ── 保留的基础设施字段 (不由前端编辑, 原样回写) ──
    if extra:
        sections.append("# ── 基础设施 (系统自动管理, 请勿手动编辑) ──")
        sections.append(_yaml.safe_dump(extra, default_flow_style=False, allow_unicode=True).strip())
        sections.append("")

    return "\n".join(sections)
