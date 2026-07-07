"""Skill prompt section cache — LRU-based with invalidation.

Any skill mutation (write, delete, install, enable/disable) must call
:func:`refresh_skills_system_prompt_cache` to invalidate the cache.
"""

from __future__ import annotations

import hashlib
import logging
from functools import lru_cache
from typing import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level cache configuration
# ---------------------------------------------------------------------------

# Max number of cached prompt sections.  Each entry is ~1-5 KB for typical
# skill sets, so 16 entries ≤ 80 KB memory — negligible.
_CACHE_MAX_SIZE = 16

# The cached function reference is replaced on each call to
# ``refresh_skills_system_prompt_cache()`` so callers don't need to
# pass a callable every time.
_cached_fn: Callable[[str], str] | None = None


def _make_cache_key(skills_signature: str) -> str:
    """Hash the skills signature to produce a compact cache key.

    ``skills_signature`` should be a stable string that uniquely identifies
    the current set of enabled skills and their versions.  Example::

        "code-reviewer:v1;deep-research:v2;…"
    """
    return hashlib.sha256(skills_signature.encode()).hexdigest()[:16]


def refresh_skills_system_prompt_cache() -> None:
    """Invalidate the skills prompt section cache.

    Call this after **any** mutation that could change the set of enabled
    skills or their content:

    * ``SkillStorage.write_custom_skill()``
    * ``SkillStorage.delete_custom_skill()``
    * ``SkillStorage.install_skill_from_archive()``
    * ``ExtensionsConfig`` enable/disable change
    * Agent skills whitelist change
    """
    global _cached_fn
    # lru_cache has no explicit invalidate — we replace the cached function
    # with a fresh one each time, so old cache entries are garbage-collected.
    _cached_fn = _build_cached_fn()
    logger.debug("Skills prompt cache invalidated")


def _build_cached_fn() -> Callable[[str], str]:
    """Build a fresh LRU-cached prompt section builder."""

    @lru_cache(maxsize=_CACHE_MAX_SIZE)
    def cached_builder(
        skills_signature: str,
        builder: Callable[[], str],
    ) -> str:
        """Cached wrapper — the builder is only called on cache miss."""
        return builder()

    def inner(skills_signature: str, builder: Callable[[], str]) -> str:
        return cached_builder(skills_signature, builder)

    return inner


# Initialise the cached function at import time.
_cached_fn = _build_cached_fn()


def get_cached_skills_prompt_section(
    skills_signature: str,
    builder: Callable[[], str],
) -> str:
    """Return a cached prompt section, building it on cache miss.

    Args:
        skills_signature: Stable string identifying the current skill set.
            Build it from skill names + versions, e.g. via
            ``";".join(f"{s.name}:{getattr(s,'version','0')}" for s in skills)``.
        builder: Zero-argument callable that produces the prompt section
            string.  Only called on cache miss.

    Returns:
        The cached (or freshly built) prompt section string.
    """
    if _cached_fn is None:
        refresh_skills_system_prompt_cache()
    assert _cached_fn is not None
    return _cached_fn(skills_signature, builder)


def build_skills_signature(skills: list) -> str:
    """Build a stable skills signature string for cache keying.

    Args:
        skills: List of ``Skill`` objects.

    Returns:
        Semicolon-delimited ``name:version`` string.
    """
    parts = []
    for s in sorted(skills, key=lambda sk: sk.name):
        version = getattr(s, "version", "0") or "0"
        parts.append(f"{s.name}:{version}")
    return ";".join(parts)
