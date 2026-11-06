"""Declarative base, column types and enumerations shared by the models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Dialect, Enum, MetaData
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

# Explicit constraint names, required by Alembic batch migrations on SQLite.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes, stored naive (SQLite has no time zone support)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            msg = "naive datetime; use timezone-aware UTC datetimes"
            raise ValueError(msg)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 (SQLAlchemy reads it as a class attribute)
        datetime: UTCDateTime(),
        dict[str, Any]: JSON(),
    }


def str_enum(enum: type[StrEnum]) -> Enum:
    """Store a StrEnum by value, as a plain string column."""
    return Enum(
        enum,
        native_enum=False,
        values_callable=lambda members: [member.value for member in members],
        length=max(len(member.value) for member in enum),
        validate_strings=True,
    )


class AssemblyStatus(StrEnum):
    PREPARATION = "preparation"
    IN_SESSION = "in_session"
    CLOSED = "closed"


class AccessMode(StrEnum):
    PASSWORD = "password"  # noqa: S105 (an access mode name, not a password)
    CODES = "codes"


class CredentialKind(StrEnum):
    DEVICE = "device"  # password mode: one random token per device
    CODE = "code"  # codes mode: one printed code per person


class BatchStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class PollStatus(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    CLOSED = "closed"
    PUBLISHED = "published"
    CANCELLED = "cancelled"


class EmailTokenKind(StrEnum):
    VERIFY = "verify"
    RESET = "reset"
    CHANGE_EMAIL = "change_email"
    INVITE = "invite"  # account created by the super-admin: the person chooses a password
