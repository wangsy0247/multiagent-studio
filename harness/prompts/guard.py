"""Prompt injection guard and input sanitizer."""
from __future__ import annotations

import re
from typing import Any


class PromptGuard:
    """Detect and sanitize prompt injection attempts."""

    INJECTION_PATTERNS = [
        r"忽略.*指令",
        r"忽略.*提示词",
        r"ignore.*previous.*instructions?",
        r"你现在的角色是",
        r"system\s*prompt",
        r"###\s*系统",
        r"<system>",
        r"</system>",
    ]

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.block_mode = self.config.get("block_mode", "fail")
        self.extra_patterns = self.config.get("extra_patterns", [])
        self.max_length = self.config.get("max_length", 10000)

    def sanitize(self, text: str) -> str:
        """Escape common injection markers and truncate overly long inputs."""
        text = text.replace("###", "\\###")
        text = text.replace("<system>", "\\<system>")
        text = text.replace("</system>", "\\</system>")
        if len(text) > self.max_length:
            text = text[: self.max_length] + "...[内容已截断]"
        return text

    def detect_injection(self, text: str) -> tuple[bool, list[str]]:
        """Return (is_injection, matched_patterns)."""
        matches = []
        all_patterns = self.INJECTION_PATTERNS + self.extra_patterns
        for pattern in all_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                matches.append(pattern)
        return bool(matches), matches
