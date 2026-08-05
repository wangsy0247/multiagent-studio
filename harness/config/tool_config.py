"""Tool configuration models for config.yaml-driven tool loading."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ToolGroupConfig(BaseModel):
    """Config section for a tool group."""

    name: str = Field(..., description="Unique name for the tool group")
    description: str = Field(default="", description="Optional group description")
    model_config = ConfigDict(extra="allow")


class ToolConfig(BaseModel):
    """Config section for a tool.

    The ``use`` field follows the harness's convention:
    ``module.path:variable_name`` (e.g. ``harness.tools.search:web_search``).
    """

    name: str = Field(..., description="Unique tool name")
    group: str = Field(..., description="Tool group name")
    use: str = Field(
        ...,
        description="Variable path like module.path:variable_name",
    )
    model_config = ConfigDict(extra="allow")
