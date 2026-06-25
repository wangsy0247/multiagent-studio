"""Agent package — Lead Agent, SubAgent, and SubAgent manager."""

from harness.agents.lead_agent import LeadAgent
from harness.agents.subagent import SubAgent
from harness.agents.subagent_manager import SubagentManager
from harness.agents.presets import PRESET_SUBAGENTS, build_subagent_config

__all__ = [
    "LeadAgent",
    "SubAgent",
    "SubagentManager",
    "PRESET_SUBAGENTS",
    "build_subagent_config",
]
