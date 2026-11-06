"""Append-only, hash-chained audit log, and the persistent email outbox."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from agaura.models.base import Base, utcnow


class AuditEntry(Base):
    """One audit event. Never contains a password, a voter code or ballot content (FR-82).

    ``user_id`` and ``assembly_id`` deliberately have no foreign key: deleting an account or a
    session must not rewrite the log, or the hash chain would break.
    """

    __tablename__ = "audit_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime]
    user_id: Mapped[int | None] = mapped_column(index=True)
    assembly_id: Mapped[int | None] = mapped_column(index=True)
    actor: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(50))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class OutboxEmail(Base):
    """An email waiting to be sent, or already sent. Retried until delivered or given up."""

    __tablename__ = "email_outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    to_address: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(200))
    body_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    next_attempt_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    sent_at: Mapped[datetime | None]
    failed_at: Mapped[datetime | None]
