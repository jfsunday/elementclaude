from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Room(Base):
    __tablename__ = "rooms"

    room_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(32), default="default", nullable=False)
    model: Mapped[str] = mapped_column(String(64), default="claude-opus-4-7", nullable=False)
    cwd: Mapped[str | None] = mapped_column(Text, nullable=True)
    claude_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_activity: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Admin(Base):
    __tablename__ = "admins"

    matrix_user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PendingApproval(Base):
    __tablename__ = "pending_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    room_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    matrix_event_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_input: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)  # allow | deny | allow_always
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    room_id: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    actor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
