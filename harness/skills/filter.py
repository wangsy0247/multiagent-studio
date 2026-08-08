"""Per-agent skill 开关过滤.

per-agent ``extensions_config.yaml`` 的 ``skills`` 是 ``dict[name, bool]``,
语义为黑名单 (与 MCP 子集一致):

- 空 dict → 全部放行 (向后兼容)
- 显式 ``false`` → 该 agent 禁用此 skill
- 缺失 → 放行

叠加顺序: 全局 extensions_config.json 开关 (storage 层) → agent config.yaml
skills 白名单 → 本过滤。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

# 请求级 per-agent skill 开关 — subagent 无独立配置, 经 contextvar 继承 parent
# (与 main.py 的 _current_req_creds 同款模式; 在请求入口处由 EffectiveConfig 设置)
_current_enabled_skills: ContextVar[dict[str, bool]] = ContextVar(
    "current_enabled_skills", default={},
)


def set_current_enabled_skills(enabled: dict[str, bool]) -> None:
    """在请求入口设置当前 agent 的 skill 黑名单 (main.py 调用)."""
    _current_enabled_skills.set(enabled or {})


def filter_skills_by_agent(
    skills: list[Any], enabled: dict[str, bool] | None
) -> list[Any]:
    """按 per-agent skill 开关过滤."""
    if not enabled:
        return skills
    return [s for s in skills if enabled.get(getattr(s, "name", ""), True)]


def filter_skills_by_current_context(skills: list[Any]) -> list[Any]:
    """按请求级 contextvar 中的 skill 开关过滤 (subagent 继承 parent 用)."""
    return filter_skills_by_agent(skills, _current_enabled_skills.get())
