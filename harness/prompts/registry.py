"""Prompt template registry."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from harness.prompts.renderer import PromptRenderer
from harness.prompts.storage import PromptStorage, PromptTemplate


class PromptRegistry:
    """Central registry for versioned prompt templates."""

    def __init__(self, storage: PromptStorage, renderer: PromptRenderer):
        self.storage = storage
        self.renderer = renderer
        self._cache: dict[str, PromptTemplate] = {}

    async def register(self, template: PromptTemplate) -> PromptTemplate:
        """Register a new template, bumping version if it already exists."""
        existing = await self.storage.get(template.id)
        if existing is not None:
            template.version = self._bump_version(existing.version)

        template.updated_at = datetime.now()
        await self.storage.save(template)
        self._cache[template.id] = template
        return template

    async def get(
        self,
        template_id: str,
        version: str | None = None,
    ) -> PromptTemplate | None:
        """Fetch a template, defaulting to the cached active version."""
        if version is None and template_id in self._cache:
            return self._cache[template_id]
        template = await self.storage.get(template_id, version)
        if version is None and template is not None:
            self._cache[template_id] = template
        return template

    async def list(
        self,
        agent_type: str | None = None,
        role: str | None = None,
        tags: list[str] | None = None,
    ) -> list[PromptTemplate]:
        """List registered templates."""
        return await self.storage.list(agent_type, role, tags)

    async def render(
        self,
        template_id: str,
        variables: dict[str, Any],
        version: str | None = None,
    ) -> str:
        """Render a template with variables."""
        template = await self.get(template_id, version)
        if template is None:
            raise ValueError(f"Template '{template_id}' not found")
        return self.renderer.render(template.content, variables)

    async def activate(self, template_id: str, version: str) -> None:
        """Activate a specific version."""
        await self.storage.activate(template_id, version)
        template = await self.storage.get(template_id, version)
        self._cache[template_id] = template

    async def update_state(
        self,
        template_id: str,
        version: str,
        new_state: str,
    ) -> None:
        """Update lifecycle state of a template version."""
        await self.storage.update_state(template_id, version, new_state)

    async def get_state(self, template_id: str, version: str) -> str:
        """Return lifecycle state of a template version."""
        template = await self.storage.get(template_id, version)
        if template is None:
            raise ValueError(f"Template {template_id}@{version} not found")
        return template.state

    async def bootstrap_defaults(self) -> list[Any]:
        """Import built-in template files into the registry."""
        if not hasattr(self.storage, "bootstrap_from_templates"):
            return []
        templates = await self.storage.bootstrap_from_templates()
        for template in templates:
            self._cache[template.id] = template
        return templates

    @staticmethod
    def _bump_version(version: str) -> str:
        parts = version.split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            return "1.0.0"
        major, minor, patch = map(int, parts)
        return f"{major}.{minor}.{patch + 1}"
