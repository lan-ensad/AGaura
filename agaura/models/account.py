"""Accounts: users, single-use email tokens, server-side login sessions.

An account created by the super-admin has no password until the invitation is accepted
(``password_hash`` is NULL): it cannot log in or ask for a password reset meanwhile. A
super-admin (``is_superadmin``, created on the command line only) manages accounts and never
owns or reaches a session.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, String, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agaura.models.base import Base, EmailTokenKind, str_enum, utcnow

if TYPE_CHECKING:
    from agaura.models.assembly import Assembly


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    email_verified_at: Mapped[datetime | None]
    password_hash: Mapped[str | None] = mapped_column(String(255))  # None: invitation pending
    display_name: Mapped[str | None] = mapped_column(String(100))
    quota_overrides: Mapped[dict[str, Any]] = mapped_column(default=dict)
    disabled_at: Mapped[datetime | None]
    last_seen_at: Mapped[datetime | None]
    # Retention (D12, ``agaura.services.retention``): when the warning before the deletion of an
    # inactive account was queued; it lapses with any later activity.
    inactivity_warned_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    is_superadmin: Mapped[bool] = mapped_column(default=False, server_default=false())
    # A demo account (FR-09a): no address that receives mail, no password, deleted with its
    # sessions once its login ends (``agaura.services.demo``).
    is_demo: Mapped[bool] = mapped_column(default=False, server_default=false())

    assemblies: Mapped[list[Assembly]] = relationship(
        back_populates="owner", cascade="all, delete-orphan", passive_deletes=True
    )


class EmailToken(Base):
    __tablename__ = "email_token"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    kind: Mapped[EmailTokenKind] = mapped_column(str_enum(EmailTokenKind))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    new_email: Mapped[str | None] = mapped_column(String(254))
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]


class UserSession(Base):
    __tablename__ = "user_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    user_agent: Mapped[str | None] = mapped_column(String(300))
