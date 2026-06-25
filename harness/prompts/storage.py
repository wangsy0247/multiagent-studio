"""Prompt storage abstract base class and data models."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PromptTemplate(BaseModel):
    """Versioned prompt template managed by the prompt engineering system."""

    id: str
    name: str
    version: str = "1.0.0"
    agent_type: str
    role: str = "system"
    content: str
    variables: list[str] = []
    tags: list[str] = []
    description: str = ""
    author: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    is_active: bool = True
    state: str = "draft"
    metadata: dict[str, Any] = {}


class PromptVersion(BaseModel):
    """Lightweight version record for a prompt template."""

    template_id: str
    version: str
    content: str
    change_log: str = ""
    evaluation_score: float | None = None
    created_at: datetime = Field(default_factory=datetime.now)


class PromptStorage(ABC):
    """Abstract backend for prompt templates."""

    @abstractmethod
    async def save(self, template: PromptTemplate) -> None:
        """Persist a template."""
        raise NotImplementedError

    @abstractmethod
    async def get(
        self,
        template_id: str,
        version: str | None = None,
    ) -> PromptTemplate | None:
        """Load a template; default to the active version."""
        raise NotImplementedError

    @abstractmethod
    async def list(
        self,
        agent_type: str | None = None,
        role: str | None = None,
        tags: list[str] | None = None,
    ) -> list[PromptTemplate]:
        """List templates filtered by metadata."""
        raise NotImplementedError

    @abstractmethod
    async def activate(self, template_id: str, version: str) -> None:
        """Mark a version as active."""
        raise NotImplementedError

    @abstractmethod
    async def update_state(
        self,
        template_id: str,
        version: str,
        new_state: str,
    ) -> None:
        """Update lifecycle state of a template version."""
        raise NotImplementedError

    @abstractmethod
    async def get_active_version(self, template_id: str) -> str | None:
        """Return the currently active version for a template id."""
        raise NotImplementedError
