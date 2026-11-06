"""Append-only, hash-chained audit log (FR-80 to FR-83).

Each entry stores the hash of the previous one and its own hash: an HMAC-SHA256, over a
canonical JSON form of its fields, with a key derived from the master secret for this purpose
only (``Keyring.audit_key``). Changing, deleting or reordering an entry breaks the chain, which
``verify_chain`` (``agaura audit verify``) detects. The key keeps someone who can write to the
database file but does not hold the master secret (a stolen backup or volume) from rewriting
entries and computing the chain again; someone who holds both can. Removing the most recent
entries is not detectable from the chain alone. Anchors (``session_anchor``) cover both cases up
to a point: the PDF minutes print the first digits of the hash of the session's last entry,
which depends on every entry before it, and ``agaura audit verify --anchor`` finds it again.

The key comes with the session: ``Database(url, audit_key=...)`` puts it in the ``info`` of each
of its sessions (``agaura.db.audit_key_of``). Recording or verifying without it raises
``AuditKeyMissingError``: there is no unkeyed fallback. A different key (a changed
``AGAURA_SECRET_KEY``) makes every existing entry fail verification.

Callers must never put a session password, a voter code, a token or ballot content in
``details`` (FR-82); voter joins and votes are never logged individually, since their order and
time would let anyone match credentials to moments.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agaura.db import audit_key_of
from agaura.models import AuditEntry, utcnow

GENESIS_HASH = "0" * 64


class Actor:
    USER = "user"  # account holder, through the web interface
    DELEGATE = "delegate"  # someone holding a delegate link of the session (FR-92)
    SYSTEM = "system"  # the application itself (timers)
    OPERATOR = "operator"  # command line


_DELEGATE_KEY = "agaura_audit_delegate"


class AuditKeyMissingError(RuntimeError):
    """The session comes from a ``Database`` built without ``audit_key``."""

    def __init__(self) -> None:
        super().__init__(
            "the audit log needs its HMAC key: build the Database with "
            "audit_key=settings.audit_key (or Keyring.audit_key)"
        )


def _key(db: Session) -> bytes:
    key = audit_key_of(db)
    if not key:
        raise AuditKeyMissingError
    return key


def act_as_delegate(db: Session, delegate_id: int) -> None:
    """Record the account holder's entries of this transaction as a delegate's.

    The console calls it when the request comes through a delegate link: services keep acting
    for the owner of the session, and each entry they record as ``Actor.USER`` gets the actor
    ``delegate`` and the delegate's id in ``details`` (never its name or token).
    """
    db.info[_DELEGATE_KEY] = delegate_id


def _canonical(entry: AuditEntry) -> bytes:
    fields = {
        "at": entry.at.isoformat(),
        "user_id": entry.user_id,
        "assembly_id": entry.assembly_id,
        "actor": entry.actor,
        "action": entry.action,
        "details": entry.details,
        "prev_hash": entry.prev_hash,
    }
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def entry_hash(entry: AuditEntry, key: bytes) -> str:
    """HMAC-SHA256 of the entry's canonical form (64 hexadecimal digits)."""
    return hmac.new(key, _canonical(entry), hashlib.sha256).hexdigest()


def record(
    db: Session,
    action: str,
    *,
    actor: str = Actor.USER,
    user_id: int | None = None,
    assembly_id: int | None = None,
    details: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> AuditEntry:
    """Append an entry. Must run in a write transaction, which serializes the chain."""
    key = _key(db)
    delegate_id = db.info.get(_DELEGATE_KEY)
    if actor == Actor.USER and delegate_id is not None:
        actor = Actor.DELEGATE
        details = {**(details or {}), "delegate": delegate_id}
    last_hash = db.scalar(select(AuditEntry.hash).order_by(AuditEntry.id.desc()).limit(1))
    entry = AuditEntry(
        at=now or utcnow(),
        user_id=user_id,
        assembly_id=assembly_id,
        actor=actor,
        action=action,
        details=details or {},
        prev_hash=last_hash or GENESIS_HASH,
    )
    entry.hash = entry_hash(entry, key)
    db.add(entry)
    db.flush()
    return entry


@dataclass(frozen=True, slots=True)
class ChainReport:
    checked: int
    broken_at: int | None  # id of the first entry that does not match, if any

    @property
    def ok(self) -> bool:
        return self.broken_at is None


def _entries(db: Session, batch: int = 1000) -> Iterator[AuditEntry]:
    after = 0
    while True:
        rows = db.scalars(
            select(AuditEntry).where(AuditEntry.id > after).order_by(AuditEntry.id).limit(batch)
        ).all()
        yield from rows
        if len(rows) < batch:
            return
        after = rows[-1].id


def verify_chain(db: Session) -> ChainReport:
    """Check every link and every entry's HMAC with the key of the session's ``Database``."""
    key = _key(db)
    expected_prev = GENESIS_HASH
    checked = 0
    for entry in _entries(db):
        intact = entry.prev_hash == expected_prev and hmac.compare_digest(
            entry.hash, entry_hash(entry, key)
        )
        if not intact:
            return ChainReport(checked=checked, broken_at=entry.id)
        expected_prev = entry.hash
        checked += 1
    return ChainReport(checked=checked, broken_at=None)


# An anchor is printed in a document that leaves the server (the PDF minutes). Finding it again
# in an intact chain shows that no entry up to it was removed or rewritten since, even by
# someone holding the key. 16 hexadecimal digits (64 bits) are beyond reach of a forger.
ANCHOR_LENGTH = 16
_HEX_DIGITS = frozenset("0123456789abcdef")


def session_anchor(db: Session, assembly_id: int) -> str | None:
    """Anchor of the session's most recent entry, None when the session has none."""
    last = db.scalar(
        select(AuditEntry.hash)
        .where(AuditEntry.assembly_id == assembly_id)
        .order_by(AuditEntry.id.desc())
        .limit(1)
    )
    return None if last is None else last[:ANCHOR_LENGTH]


def is_anchor(text: str) -> bool:
    """Lowercase hexadecimal, from ``ANCHOR_LENGTH`` digits to a whole hash."""
    return ANCHOR_LENGTH <= len(text) <= len(GENESIS_HASH) and _HEX_DIGITS.issuperset(text)


def find_anchor(db: Session, anchor: str) -> int | None:
    """Id of the entry whose hash starts with ``anchor`` (checked with ``is_anchor``)."""
    return db.scalar(
        select(AuditEntry.id)
        .where(AuditEntry.hash.startswith(anchor, autoescape=True))
        .order_by(AuditEntry.id)
        .limit(1)
    )


def session_log(db: Session, assembly_id: int) -> list[AuditEntry]:
    return list(
        db.scalars(
            select(AuditEntry).where(AuditEntry.assembly_id == assembly_id).order_by(AuditEntry.id)
        )
    )
