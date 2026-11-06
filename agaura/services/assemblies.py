"""Assembly sessions: creation, details, lifecycle (SPEC §7), owner isolation (FR-07).

Every admin function takes the current ``owner`` and only ever sees that account's sessions; a
session of another account raises ``NotFoundError``, exactly like a missing one.
"""

from __future__ import annotations

import datetime as dt
import math
import unicodedata
from dataclasses import dataclass
from fractions import Fraction

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agaura.db import compact_after_commit
from agaura.models import AccessMode, Assembly, AssemblyStatus, Poll, PollStatus, User, utcnow
from agaura.security import (
    CROCKFORD_ALPHABET,
    PUBLIC_ID_LENGTH,
    Keyring,
    new_public_id,
    new_session_password,
    new_token,
    normalize_secret,
)
from agaura.services import audit
from agaura.services.accounts import quotas
from agaura.services.errors import (
    InvalidInputError,
    InvalidStateError,
    NotFoundError,
    QuotaExceededError,
)

MAX_TITLE_LENGTH = 200
MAX_PLACE_LENGTH = 200
MAX_QUORUM_RULE_LENGTH = 20
# Control characters (ESC, NUL…) have no place in a title or a label: they would reach the
# exports, the minutes, and a terminal showing a JSON export. Tabs and line breaks stay.
KEPT_CONTROLS = frozenset("\t\n\r")


@dataclass(frozen=True, slots=True)
class AssemblyInput:
    title: str
    date: dt.date | None = None
    place: str | None = None
    registered_count: int | None = None  # registered members, for the quorum (D7)
    # Quorum: a number of attendees ("50") or a share of registered members ("1/4").
    quorum_rule: str | None = None


def clean_text(value: str | None, *, max_length: int, field: str, required: bool) -> str | None:
    text = (value or "").strip()
    if not text:
        if required:
            raise InvalidInputError("required", field)
        return None
    if len(text) > max_length:
        raise InvalidInputError("too_long", field)
    if any(unicodedata.category(c) == "Cc" and c not in KEPT_CONTROLS for c in text):
        raise InvalidInputError("control_characters", field)
    return text


def parse_quorum_rule(raw: str | None) -> str | None:
    rule = "".join((raw or "").split())
    if not rule:
        return None
    try:
        if "/" in rule:
            share = Fraction(rule)
            valid = 0 < share <= 1
            rule = f"{share.numerator}/{share.denominator}"
        else:
            valid = rule.isdigit() and int(rule) > 0
            rule = str(int(rule)) if valid else rule
    except (ValueError, ZeroDivisionError):
        valid = False
    if not valid or len(rule) > MAX_QUORUM_RULE_LENGTH:
        raise InvalidInputError("invalid_quorum_rule")
    return rule


def quorum_required(assembly: Assembly) -> int | None:
    """Attendees needed for the quorum, or None when unknown (no rule, or no member count)."""
    rule = assembly.quorum_rule
    if not rule:
        return None
    if "/" not in rule:
        return int(rule)
    if assembly.registered_count is None:
        return None
    return math.ceil(Fraction(rule) * assembly.registered_count)


def _validated(data: AssemblyInput) -> AssemblyInput:
    if data.registered_count is not None and data.registered_count < 1:
        raise InvalidInputError("invalid_registered_count")
    return AssemblyInput(
        title=clean_text(data.title, max_length=MAX_TITLE_LENGTH, field="title", required=True)
        or "",
        date=data.date,
        place=clean_text(data.place, max_length=MAX_PLACE_LENGTH, field="place", required=False),
        registered_count=data.registered_count,
        quorum_rule=parse_quorum_rule(data.quorum_rule),
    )


def normalize_public_id(raw: str) -> str | None:
    """Session ID typed or read from a URL, or None if it cannot be one."""
    candidate = normalize_secret(raw)
    if len(candidate) != PUBLIC_ID_LENGTH or not set(candidate) <= set(CROCKFORD_ALPHABET):
        return None
    return candidate


def _unused_public_id(db: Session) -> str:
    while True:
        candidate = new_public_id()
        if db.scalar(select(Assembly.id).where(Assembly.public_id == candidate)) is None:
            return candidate


def create_assembly(
    db: Session,
    keyring: Keyring,
    owner: User,
    data: AssemblyInput,
    *,
    access_mode: AccessMode = AccessMode.PASSWORD,
    now: dt.datetime | None = None,
) -> Assembly:
    now = now or utcnow()
    if owner.is_superadmin:  # the super-admin manages accounts, never sessions
        raise InvalidStateError("superadmin_no_sessions")
    open_count = db.scalar(
        select(func.count())
        .select_from(Assembly)
        .where(Assembly.owner_id == owner.id, Assembly.status != AssemblyStatus.CLOSED)
    )
    if (open_count or 0) >= quotas(owner).open_assemblies:
        raise QuotaExceededError("open_assemblies")
    data = _validated(data)
    assembly = Assembly(
        owner_id=owner.id,
        public_id=_unused_public_id(db),
        title=data.title,
        date=data.date,
        place=data.place,
        registered_count=data.registered_count,
        quorum_rule=data.quorum_rule,
        access_mode=access_mode,
        access_password_enc=keyring.encrypt(new_session_password()),
        screen_token_enc=keyring.encrypt(new_token()),
        created_at=now,
        is_demo=owner.is_demo,
    )
    db.add(assembly)
    db.flush()
    audit.record(
        db,
        "assembly.created",
        user_id=owner.id,
        assembly_id=assembly.id,
        details={"public_id": assembly.public_id, "title": assembly.title},
        now=now,
    )
    return assembly


def list_assemblies(db: Session, owner: User) -> list[Assembly]:
    """The account's sessions, most recent first (FR-10)."""
    return list(
        db.scalars(
            select(Assembly)
            .where(Assembly.owner_id == owner.id)
            .order_by(Assembly.created_at.desc(), Assembly.id.desc())
        )
    )


def get_assembly(db: Session, owner: User, public_id: str) -> Assembly:
    normalized = normalize_public_id(public_id)
    assembly = db.scalar(
        select(Assembly).where(Assembly.public_id == normalized, Assembly.owner_id == owner.id)
    )
    if assembly is None:
        raise NotFoundError("assembly")
    return assembly


def find_assembly(db: Session, public_id: str) -> Assembly | None:
    """Any session, whatever its owner, even disabled (the operator's commands)."""
    normalized = normalize_public_id(public_id)
    if normalized is None:
        return None
    return db.scalar(select(Assembly).where(Assembly.public_id == normalized))


def find_public_assembly(db: Session, public_id: str) -> Assembly | None:
    """Voter side (voter pages, projection screen, public results): a session whose account
    the operator has not disabled (``agaura user disable``). A disabled account's sessions look
    unknown, nothing deleted: they come back with the account."""
    assembly = find_assembly(db, public_id)
    if assembly is None or assembly.owner.disabled_at is not None:
        return None
    return assembly


def require_status(assembly: Assembly, *allowed: AssemblyStatus) -> None:
    if assembly.status not in allowed:
        raise InvalidStateError(f"assembly_{assembly.status.value}")


def update_assembly(
    db: Session, owner: User, public_id: str, data: AssemblyInput, *, now: dt.datetime | None = None
) -> Assembly:
    """Edit the details; only while in preparation (FR-13)."""
    assembly = get_assembly(db, owner, public_id)
    require_status(assembly, AssemblyStatus.PREPARATION)
    data = _validated(data)
    assembly.title = data.title
    assembly.date = data.date
    assembly.place = data.place
    assembly.registered_count = data.registered_count
    assembly.quorum_rule = data.quorum_rule
    audit.record(
        db, "assembly.updated", user_id=owner.id, assembly_id=assembly.id, details={}, now=now
    )
    return assembly


def _attendance_counts(db: Session, assembly: Assembly) -> dict[str, int | None]:
    """Attendance reading for the audit entry (``services/attendance.py``)."""
    from agaura.services import attendance  # imported here: attendance builds on this module

    return attendance.counts(db, assembly)


def open_assembly(
    db: Session, owner: User, public_id: str, *, now: dt.datetime | None = None
) -> Assembly:
    now = now or utcnow()
    assembly = get_assembly(db, owner, public_id)
    require_status(assembly, AssemblyStatus.PREPARATION)
    assembly.status = AssemblyStatus.IN_SESSION
    assembly.opened_at = now
    audit.record(
        db,
        "assembly.opened",
        user_id=owner.id,
        assembly_id=assembly.id,
        details=_attendance_counts(db, assembly),
        now=now,
    )
    return assembly


def close_assembly(
    db: Session, owner: User, public_id: str, *, now: dt.datetime | None = None
) -> Assembly:
    """End the session: every voter credential stops working. Refused while a poll is open."""
    now = now or utcnow()
    assembly = get_assembly(db, owner, public_id)
    require_status(assembly, AssemblyStatus.IN_SESSION)
    open_polls = db.scalar(
        select(func.count())
        .select_from(Poll)
        .where(Poll.assembly_id == assembly.id, Poll.status == PollStatus.OPEN)
    )
    if open_polls:
        raise InvalidStateError("polls_still_open")
    details = _attendance_counts(db, assembly)  # counted while still in session
    assembly.status = AssemblyStatus.CLOSED
    assembly.closed_at = now
    from agaura.services import delegates  # imported here: delegates builds on this module

    delegates.end_all(db, assembly, now=now)  # their links end with the session (FR-92)
    # Every poll was closed (and compacted) already: a second chance if that compaction failed.
    compact_after_commit(db)
    audit.record(
        db, "assembly.closed", user_id=owner.id, assembly_id=assembly.id, details=details, now=now
    )
    return assembly


def delete_assembly(
    db: Session, owner: User, public_id: str, *, now: dt.datetime | None = None
) -> None:
    """Delete a session with everything in it; refused while in session (FR-13)."""
    assembly = get_assembly(db, owner, public_id)
    require_status(assembly, AssemblyStatus.PREPARATION, AssemblyStatus.CLOSED)
    audit.record(
        db,
        "assembly.deleted",
        user_id=owner.id,
        assembly_id=assembly.id,
        details={"public_id": assembly.public_id, "title": assembly.title},
        now=now,
    )
    db.delete(assembly)
    db.flush()
    compact_after_commit(db)


def count_in_session(db: Session) -> int:
    """Sessions in session, for the deployment guard (``/health``): demo sessions (FR-09a)
    never hold a deployment back."""
    return (
        db.scalar(
            select(func.count())
            .select_from(Assembly)
            .where(Assembly.status == AssemblyStatus.IN_SESSION, Assembly.is_demo.is_(False))
        )
        or 0
    )
