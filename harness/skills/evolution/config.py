"""Skill evolution configuration — adapted from Hermes skill_evolution_config.

Controls background review fork behaviour, usage counters, and lifecycle
transitions.  All fields are runtime-configurable via config.yaml or
environment variables.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SkillEvolutionConfig(BaseModel):
    """Configuration for agent-managed skill evolution.

    The background review fork is the core mechanism — after every N tool
    iterations without a skill_manage call, a lightweight subagent reviews
    the conversation and creates / patches skills in the background.
    """

    enabled: bool = Field(
        default=True,
        description="Master switch for skill self-evolution.",
    )
    creation_nudge_interval: int = Field(
        default=10,
        description=(
            "Number of tool-call iterations before triggering a background "
            "skill review.  Counted per turn; reset to 0 whenever the "
            "agent calls skill_manage."
        ),
    )
    max_review_turns: int = Field(
        default=16,
        description="Maximum turns for the background review subagent.",
    )
    review_timeout_seconds: int = Field(
        default=600,
        description="Wall-clock timeout for the review subagent (10 min).",
    )
    stale_after_days: int = Field(
        default=30,
        description="Days without use before a skill transitions active → stale.",
    )
    archive_after_days: int = Field(
        default=90,
        description="Days without use before a skill transitions stale → archived.",
    )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_evolution_config: SkillEvolutionConfig | None = None


def get_evolution_config() -> SkillEvolutionConfig:
    """Return the global evolution config, loading defaults if needed."""
    global _evolution_config
    if _evolution_config is None:
        _evolution_config = SkillEvolutionConfig()
    return _evolution_config


def set_evolution_config(cfg: SkillEvolutionConfig) -> None:
    """Override the global evolution config."""
    global _evolution_config
    _evolution_config = cfg
