"""Output validator for agent responses."""
from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import AIMessage

from harness.models import ValidationResult
from harness.prompts.storage import PromptTemplate


class OutputValidator:
    """Validate that an agent output conforms to template constraints."""

    FORBIDDEN_KEYWORDS = [
        "ignore previous instructions",
        "system prompt",
        "忽略之前的指令",
    ]

    def validate(
        self,
        response: AIMessage,
        template: PromptTemplate,
    ) -> ValidationResult:
        """Validate ``response`` against ``template`` constraints."""
        issues: list[str] = []
        content = str(response.content)

        if self._contains_forbidden(content, template):
            issues.append("输出包含禁止内容")

        allowed_tools = set(template.metadata.get("allowed_tools", []))
        for tc in response.tool_calls or []:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            if allowed_tools and name not in allowed_tools:
                issues.append(f"非法工具调用: {name}")

        required_format = template.metadata.get("required_format")
        if required_format and not self._match_format(content, required_format):
            issues.append("输出格式不符合要求")

        return ValidationResult(valid=len(issues) == 0, issues=issues)

    def _contains_forbidden(self, content: str, template: PromptTemplate) -> bool:
        custom = set(template.metadata.get("forbidden_keywords", []))
        for kw in self.FORBIDDEN_KEYWORDS + list(custom):
            if kw.lower() in content.lower():
                return True
        return False

    def _match_format(self, content: str, fmt: str) -> bool:
        try:
            return bool(re.search(fmt, content, re.DOTALL))
        except re.error:
            return False
