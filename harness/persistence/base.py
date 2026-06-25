"""SQLAlchemy declarative base — shared by all ORM models."""
from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base with ``to_dict()`` convenience method."""

    def to_dict(self, *, exclude: set[str] | None = None) -> dict:
        """Convert ORM row to a plain dict."""
        exclude = exclude or set()
        return {
            c.key: getattr(self, c.key)
            for c in inspect(self).mapper.column_attrs
            if c.key not in exclude
        }

    def __repr__(self) -> str:
        cols = ", ".join(
            f"{c.key}={getattr(self, c.key)!r}"
            for c in inspect(self).mapper.column_attrs
        )
        return f"{type(self).__name__}({cols})"
