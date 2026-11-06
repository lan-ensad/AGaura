"""Delegate links (FR-92): some console rights of one session, handed out without an account.

The tribune creates a link for a person, with a name typed freely and one or more rights
(``Right``). Whoever opens the link gets those rights on that session only, from any number of
devices, until the tribune revokes the link or closes the session. The link carries a 256-bit
token in its fragment; the device keeps it in a cookie, which ``authenticate`` checks on every
request.

The owner functions take the current ``owner`` and only reach that account's sessions (FR-07).
Audit entries name a delegate by its id, never by its name, and never contain its token.
Actions taken through a link are recorded as the owner's, with the actor ``delegate`` and the
delegate's id (``audit.act_as_delegate``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from agaura.models import Assembly, AssemblyStatus, Delegate, User, utcnow
from agaura.security import Keyring, hash_token, new_token
from agaura.services import audit
from agaura.services.assemblies import find_assembly, get_assembly, require_status
from agaura.services.errors import InvalidInputError, NotFoundError, QuotaExceededError

MAX_DELEGATES = 20  # links in use per session
MAX_NAME_LENGTH = 100
TOUCH_INTERVAL = dt.timedelta(minutes=5)  # how often ``last_seen_at`` is written


class Right(StrEnum):
    """What a delegate link may do in the console, in display order."""

    AGENDA = "agenda"  # agenda and the item under discussion
    NOTES = "notes"  # session notes, internal ones included (often the secretary's)
    POLLS = "polls"  # create, edit, push, close, cancel polls; publish their results
    TIMER = "timer"  # chronometer of the projection screen
    SCREEN = "screen"  # what the room sees: show or hide each block of the projection screen
    HEADCOUNT = "headcount"  # people counted in the room (FR-28)


RIGHTS = tuple(Right)
ACTIVE_STATES = (AssemblyStatus.PREPARATION, AssemblyStatus.IN_SESSION)


def clean_rights(raw: Iterable[str]) -> list[str]:
    """The rights asked for, known ones only, in display order; at least one."""
    wanted = set(raw)
    rights = [right.value for right in RIGHTS if right.value in wanted]
    if not rights or not wanted <= {right.value for right in RIGHTS}:
        raise InvalidInputError("delegates_rights_required", "rights")
    return rights


def clean_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise InvalidInputError("delegates_name_required", "name")
    if len(name) > MAX_NAME_LENGTH:
        raise InvalidInputError("delegates_name_too_long", "name")
    return name


def _in_use(db: Session, assembly: Assembly) -> list[Delegate]:
    return list(
        db.scalars(
            select(Delegate)
            .where(Delegate.assembly_id == assembly.id, Delegate.ended_at.is_(None))
            .order_by(Delegate.id)
        )
    )


def list_delegates(db: Session, owner: User, public_id: str) -> list[Delegate]:
    """The links in use, oldest first."""
    return _in_use(db, get_assembly(db, owner, public_id))


def names(db: Session, assembly: Assembly) -> dict[int, str]:
    """Every delegate of the session, ended ones included, by id: names the session log."""
    rows = db.execute(
        select(Delegate.id, Delegate.name).where(Delegate.assembly_id == assembly.id)
    ).all()
    return dict(rows)


def create_delegate(
    db: Session,
    keyring: Keyring,
    owner: User,
    public_id: str,
    name: str,
    rights: Iterable[str],
    *,
    now: dt.datetime | None = None,
) -> tuple[Delegate, str]:
    """A new link; returns it with its token (also kept encrypted, to be shown again)."""
    assembly = get_assembly(db, owner, public_id)
    require_status(assembly, *ACTIVE_STATES)
    delegate_name, delegate_rights = clean_name(name), clean_rights(rights)
    if len(_in_use(db, assembly)) >= MAX_DELEGATES:
        raise QuotaExceededError("delegates_quota")
    token = new_token()
    delegate = Delegate(
        assembly_id=assembly.id,
        name=delegate_name,
        rights=delegate_rights,
        token_hash=hash_token(token),
        token_enc=keyring.encrypt(token),
        created_at=now or utcnow(),
    )
    db.add(delegate)
    db.flush()
    audit.record(
        db,
        "delegate.created",
        user_id=owner.id,
        assembly_id=assembly.id,
        details={"delegate": delegate.id, "rights": delegate_rights},
        now=now,
    )
    return delegate, token


def _owned(db: Session, owner: User, public_id: str, delegate_id: int) -> Delegate:
    """A link in use of a session of ``owner``; another session's, or an ended one, is 404."""
    assembly = get_assembly(db, owner, public_id)
    delegate = db.get(Delegate, delegate_id)
    if delegate is None or delegate.assembly_id != assembly.id or delegate.ended_at is not None:
        raise NotFoundError("delegate")
    return delegate


def update_delegate(
    db: Session,
    owner: User,
    public_id: str,
    delegate_id: int,
    name: str,
    rights: Iterable[str],
    *,
    now: dt.datetime | None = None,
) -> Delegate:
    """Rename a link or change its rights; the link itself stays the same."""
    delegate = _owned(db, owner, public_id, delegate_id)
    require_status(delegate.assembly, *ACTIVE_STATES)
    new_name, new_rights = clean_name(name), clean_rights(rights)
    if (new_name, new_rights) != (delegate.name, delegate.rights):
        delegate.name, delegate.rights = new_name, new_rights
        audit.record(
            db,
            "delegate.updated",
            user_id=owner.id,
            assembly_id=delegate.assembly_id,
            details={"delegate": delegate.id, "rights": new_rights},
            now=now,
        )
    return delegate


def _end(delegate: Delegate, now: dt.datetime) -> None:
    delegate.ended_at = now
    delegate.token_hash = None
    delegate.token_enc = None


def revoke_delegate(
    db: Session, owner: User, public_id: str, delegate_id: int, *, now: dt.datetime | None = None
) -> None:
    """End a link at once: every device that opened it loses access at its next request."""
    now = now or utcnow()
    delegate = _owned(db, owner, public_id, delegate_id)
    _end(delegate, now)
    db.flush()
    audit.record(
        db,
        "delegate.revoked",
        user_id=owner.id,
        assembly_id=delegate.assembly_id,
        details={"delegate": delegate.id},
        now=now,
    )


def end_all(db: Session, assembly: Assembly, *, now: dt.datetime | None = None) -> int:
    """End every link of a session (called when it closes); returns how many were in use."""
    now = now or utcnow()
    ended = _in_use(db, assembly)
    for delegate in ended:
        _end(delegate, now)
    db.flush()
    return len(ended)


def link_token(keyring: Keyring, delegate: Delegate) -> str:
    """The token of a link in use, to show the link again in the owner's console."""
    if delegate.token_enc is None:
        raise NotFoundError("delegate")
    return keyring.decrypt(delegate.token_enc)


def authenticate(db: Session, public_id: str, token: str | None) -> Delegate | None:
    """The link in use behind ``token`` on this session, or None.

    None as well when the session is closed or its owner's account is disabled. The token is
    compared through its digest: a 256-bit secret needs no constant-time comparison.
    """
    if not token:
        return None
    assembly = find_assembly(db, public_id)
    if (
        assembly is None
        or assembly.status not in ACTIVE_STATES
        or assembly.owner.disabled_at is not None
        or assembly.owner.is_superadmin
    ):
        return None
    return db.scalar(
        select(Delegate).where(
            Delegate.assembly_id == assembly.id,
            Delegate.ended_at.is_(None),
            Delegate.token_hash == hash_token(token),
        )
    )


def needs_touch(delegate: Delegate, now: dt.datetime | None = None) -> bool:
    now = now or utcnow()
    return delegate.last_seen_at is None or now - delegate.last_seen_at >= TOUCH_INTERVAL


def touch(db: Session, delegate_id: int, *, now: dt.datetime | None = None) -> None:
    """Record that the link was used (shown to the tribune as its last activity)."""
    delegate = db.get(Delegate, delegate_id)
    if delegate is not None and delegate.ended_at is None:
        delegate.last_seen_at = now or utcnow()
        db.flush()
