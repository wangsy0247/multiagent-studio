"""Skill write origin tracking — ContextVar pattern.

Distinguishes between skills created by the background review fork,
the curator, and user-initiated actions.  Only "background_review" and
"curator" origins mark skills as curator-managed (created_by="agent").

``asyncio.create_task()`` copies the parent context, so the origin set in
a background task does NOT leak into the main conversation.
"""

from __future__ import annotations

from contextvars import ContextVar

_skill_write_origin: ContextVar[str] = ContextVar(
    "skill_write_origin", default="user",
)

# Valid origins
ORIGIN_USER = "user"
ORIGIN_BACKGROUND_REVIEW = "background_review"
ORIGIN_CURATOR = "curator"


def set_write_origin(origin: str) -> None:
    """Set the write origin for the current async context.

    Args:
        origin: One of ``ORIGIN_USER``, ``ORIGIN_BACKGROUND_REVIEW``,
                or ``ORIGIN_CURATOR``.
    """
    _skill_write_origin.set(origin)


def get_write_origin() -> str:
    """Return the current write origin (defaults to ``"user"``)."""
    return _skill_write_origin.get()


def is_curator_managed() -> bool:
    """Return True when the current origin opts into curator management."""
    return get_write_origin() in (ORIGIN_BACKGROUND_REVIEW, ORIGIN_CURATOR)
