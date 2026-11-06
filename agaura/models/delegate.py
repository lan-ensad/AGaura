"""Delegate links: part of the session console handed to someone without an account (FR-92).

A delegate link belongs to one session and grants some of its console rights (agenda and
notes, polls, chronometer, headcount) to whoever holds it. Its secret is a 256-bit token: the
row keeps its SHA-256 digest, to recognize the cookie of a device that opened the link, and an
encrypted copy, so the tribune can show the link or its QR code again. Revoking the link, or
closing the session, clears both: the row stays only to name the delegate in the session log.

Delegates are members of the tribune, never voters: nothing here relates to a ballot.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agaura.models.base import Base, utcnow

if TYPE_CHECKING:
    from agaura.models.assembly import Assembly


class Delegate(Base):
    __tablename__ = "delegate"

    id: Mapped[int] = mapped_column(primary_key=True)
    assembly_id: Mapped[int] = mapped_column(
        ForeignKey("assembly.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))  # typed freely by the tribune
    # Console rights, values of ``services.delegates.Right``, in their display order.
    rights: Mapped[list[str]] = mapped_column(JSON, default=list)
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    token_enc: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[dt.datetime | None]
    ended_at: Mapped[dt.datetime | None]  # revoked, or the session closed

    assembly: Mapped[Assembly] = relationship()
