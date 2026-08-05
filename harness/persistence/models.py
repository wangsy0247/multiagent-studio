"""ORM models — RunRow, ThreadMetaRow, RunEventRow.

Table names are aligned with the harness design (``runs``, ``threads_meta``, ``run_events``).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    Index,
)
from sqlalchemy.dialects.postgresql import JSON

from harness.persistence.base import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ThreadMetaRow(Base):
    """Per-thread metadata — display name, status, owner."""

    __tablename__ = "threads_meta"

    thread_id: str = Column(String(64), primary_key=True)
    user_id: str = Column(String(64), index=True, nullable=True)
    display_name: str = Column(String(256), nullable=True)
    status: str = Column(String(20), default="idle")
    metadata_json: dict = Column(JSON, default=dict)
    created_at: datetime = Column(DateTime(timezone=True), default=_utc_now)
    updated_at: datetime = Column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )

    __table_args__ = (
        Index("ix_threads_meta_user_status", "user_id", "status"),
    )


class RunRow(Base):
    """Per-run metadata — status, tokens, messages, error info."""

    __tablename__ = "runs"

    run_id: str = Column(String(64), primary_key=True)
    thread_id: str = Column(String(64), index=True, nullable=False)
    user_id: str = Column(String(64), index=True, nullable=True)
    status: str = Column(String(20), default="pending")
    model_name: str = Column(String(128), nullable=True)
    first_human_message: str = Column(Text, nullable=True)
    last_ai_message: str = Column(Text, nullable=True)
    error: str = Column(Text, nullable=True)
    message_count: int = Column(Integer, default=0)
    total_input_tokens: int = Column(Integer, default=0)
    total_output_tokens: int = Column(Integer, default=0)
    total_tokens: int = Column(Integer, default=0)
    llm_call_count: int = Column(Integer, default=0)
    metadata_json: dict = Column(JSON, default=dict)
    created_at: datetime = Column(DateTime(timezone=True), default=_utc_now)
    updated_at: datetime = Column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )

    __table_args__ = (
        Index("ix_runs_thread_status", "thread_id", "status"),
    )


class RunEventRow(Base):
    """Per-run events — LLM responses, tool results, lifecycle markers."""

    __tablename__ = "run_events"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    thread_id: str = Column(String(64), nullable=False)
    run_id: str = Column(String(64), nullable=False)
    user_id: str = Column(String(64), index=True, nullable=True)
    event_type: str = Column(String(32), nullable=False)
    category: str = Column(String(16), default="trace")
    content: str = Column(Text, default="")
    event_metadata: dict = Column(JSON, default=dict)
    seq: int = Column(Integer, nullable=False)
    created_at: datetime = Column(DateTime(timezone=True), default=_utc_now)

    __table_args__ = (
        Index("ix_events_thread_cat_seq", "thread_id", "category", "seq"),
        Index("ix_events_run", "thread_id", "run_id", "seq"),
    )
