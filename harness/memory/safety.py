"""Memory file safety validator — detects prompt injection, credential exfiltration,
and invisible Unicode characters in user-editable memory files before they are
injected into the system prompt.

Adapted from Hermes Agent's ``_scan_context_content()`` design.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ── Threat patterns ─────────────────────────────────────────────────────────

# Layer 1: prompt injection / instruction override
_INJECTION_PATTERNS: list[tuple[str, str]] = [
    (r'ignore\s+(previous|all|above|prior)\s+instructions', "prompt_injection"),
    (r'ignore\s+all\s+previous\s+instructions', "prompt_injection"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (
        r'act\s+as\s+(if|though)\s+you\s+(have\s+no|don\'t\s+have)\s+'
        r'(restrictions|limits|rules)',
        "bypass_restrictions",
    ),
    # HTML hidden injection
    (r'<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->', "html_comment_injection"),
    (r'<\s*div\s+style\s*=\s*["\'][\s\S]*?display\s*:\s*none', "hidden_div"),
    # Translate + execute injection
    (r'translate\s+.*\s+into\s+.*\s+and\s+(execute|run|eval)', "translate_execute"),
]

# Layer 2: credential exfiltration / SSH backdoor
_EXFIL_PATTERNS: list[tuple[str, str]] = [
    # curl-based exfiltration
    (
        r'curl\s+[^\n]*\$?\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)',
        "exfil_curl",
    ),
    (
        r'curl\s+[^\n]*https?://[^\s]+.*\$?\{?\w*(?:KEY|TOKEN|SECRET)',
        "exfil_curl_remote",
    ),
    # Reading sensitive files
    (
        r'cat\s+[^\n]*(?:\.env|credentials|\.netrc|\.pgpass|id_rsa|id_ed25519)',
        "read_secrets",
    ),
    # SSH backdoor
    (r'ssh\s+[^\n]*-o\s*ProxyCommand', "ssh_backdoor"),
    (r'ssh\s+[^\n]*-o\s*RemoteForward\s+[^\n]*:22', "ssh_backdoor_reverse"),
    # Netcat backdoor
    (r'nc\s+-[ln]+\s+\d+\s+-[ec]+\s+/(?:bin|usr)/', "netcat_backdoor"),
    # Environment variable exfiltration
    (r'(?:printenv|env\s*\|)\s*.*\$?\{?\w*(?:KEY|TOKEN|SECRET)', "env_exfil"),
]

# Layer 3: invisible Unicode characters (zero-width, bidirectional control)
_INVISIBLE_UNICODE: set[str] = {
    '​',  # ZERO WIDTH SPACE
    '‌',  # ZERO WIDTH NON-JOINER
    '‍',  # ZERO WIDTH JOINER
    '⁠',  # WORD JOINER
    '﻿',  # ZERO WIDTH NO-BREAK SPACE / BOM
    '‪',  # LEFT-TO-RIGHT EMBEDDING
    '‫',  # RIGHT-TO-LEFT EMBEDDING
    '‬',  # POP DIRECTIONAL FORMATTING
    '‭',  # LEFT-TO-RIGHT OVERRIDE
    '‮',  # RIGHT-TO-LEFT OVERRIDE
}

_ALL_PATTERNS = _INJECTION_PATTERNS + _EXFIL_PATTERNS


def validate_content(content: str, source: str = "memory") -> list[str]:
    """Scan *content* for threats and invisible characters.

    Args:
        content: The text to scan (a single string, e.g. a fact body or summary).
        source: Label for log messages (e.g. ``"memory.json"``, ``"USER.json"``).

    Returns:
        A list of finding identifiers (empty = clean).
        Possible values include ``"prompt_injection"``, ``"exfil_curl"``,
        ``"invisible_unicode"``, etc.
    """
    findings: list[str] = []

    # --- Layer 1 + 2: regex threat patterns ---
    for pattern, pid in _ALL_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            findings.append(pid)

    # --- Layer 3: invisible Unicode ---
    for char in _INVISIBLE_UNICODE:
        if char in content:
            findings.append(f"invisible_unicode_U+{ord(char):04X}")

    if findings:
        logger.warning(
            "Memory safety: blocked %s — findings=%s",
            source,
            ", ".join(findings),
        )

    return findings


def validate_memory_json(memory_data: dict, source: str = "memory.json") -> list[str]:
    """Recursively validate every string value in *memory_data*.

    Walks the memory JSON tree (``user`` summaries, ``history`` summaries,
    ``facts[].content``, etc.) and collects all safety findings.

    Args:
        memory_data: The parsed memory JSON dict.
        source: Label for log messages.

    Returns:
        A flat list of all finding identifiers across the entire JSON.
    """
    all_findings: list[str] = []
    _walk_strings(memory_data, source, all_findings)
    return all_findings


def _walk_strings(obj: object, source: str, findings: list[str]) -> None:
    """Recursively find every ``str`` value in *obj* and run ``validate_content``."""
    if isinstance(obj, str):
        findings.extend(validate_content(obj, source))
    elif isinstance(obj, dict):
        for value in obj.values():
            _walk_strings(value, source, findings)
    elif isinstance(obj, list):
        for item in obj:
            _walk_strings(item, source, findings)


def sanitize_memory_if_unsafe(
    memory_data: dict,
    source: str = "memory.json",
) -> tuple[dict, list[str]]:
    """Validate *memory_data* and return an empty memory if any threat is found.

    This is the primary entry point for ``FileMemoryStorage.load()`` — it
    returns either the original data (clean) or a fresh empty structure
    (threat detected), plus the findings list for logging.

    Args:
        memory_data: The parsed memory JSON dict.
        source: Label for log messages.

    Returns:
        ``(memory_data_or_empty, findings_list)``
    """
    findings = validate_memory_json(memory_data, source)
    if not findings:
        return memory_data, []

    logger.error(
        "Memory safety violation in %s — returning empty memory. Findings: %s",
        source,
        ", ".join(findings),
    )
    # Return empty memory — caller should use create_empty_memory()
    return {}, findings
