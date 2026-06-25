"""Jinja2 prompt renderer."""
from __future__ import annotations

from typing import Any

from jinja2 import BaseLoader, Environment, TemplateError


class PromptRenderer:
    """Render prompt templates using Jinja2."""

    def __init__(self):
        self.env = Environment(loader=BaseLoader(), autoescape=False)
        self.env.filters["truncate"] = self._truncate

    @staticmethod
    def _truncate(value: str, length: int = 200, suffix: str = "...") -> str:
        text = str(value)
        if len(text) <= length:
            return text
        return text[:length].rsplit(" ", 1)[0] + suffix

    def render(self, content: str, variables: dict[str, Any] | None = None) -> str:
        """Render a Jinja2 template string with the supplied variables."""
        try:
            template = self.env.from_string(content)
            return template.render(variables or {})
        except TemplateError as exc:
            return f"[render error] {exc}"
