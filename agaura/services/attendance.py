"""Attendance readings: how many people were in the room, and connected, at key moments (FR-28).

The number of people in the room changes during an assembly. So that the tribune can follow
arrivals and departures, a **reading** is taken at a few moments chosen by the tribune or by
the course of the session: when the headcount is saved, when a poll is opened or closed (by
hand or by its timer), and when the session opens and closes. A reading holds the headcount
entered by the tribune (None when none) and the connections at that moment
(``access.attendees``, the cumulative count shown as "Attendees").

Secrecy (SPEC §9.2): readings are aggregated counts. Nothing is ever recorded when a voter
joins or votes, and no reading refers to a credential: their order and time would let anyone
match credentials to moments.

Readings are not a table of their own: the audit entries of those actions, already timestamped
and hash-chained, carry the counts in their details (``attendees``, ``headcount``; see
``counts``). Entries written before the counts were added read as blanks (None).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from agaura.models import Assembly, AuditEntry, User
from agaura.services import access, assemblies

# Audit action → event code of a reading (also the code used in the JSON export).
EVENTS = {
    "assembly.opened": "session_opened",
    "access.headcount_set": "headcount_set",
    "poll.opened": "poll_opened",
    "poll.closed": "poll_closed",
    "assembly.closed": "session_closed",
}


@dataclass(frozen=True, slots=True)
class Reading:
    at: dt.datetime
    event: str  # a value of ``EVENTS``
    headcount: int | None  # people counted in the room, as last entered by the tribune
    attendees: int | None  # connections: voter accesses in use (FR-26)
    poll_id: int | None = None  # the poll opened or closed


def counts(db: Session, assembly: Assembly) -> dict[str, int | None]:
    """The counts to store in the audit entry of an action that takes a reading."""
    return {"attendees": access.attendees(db, assembly), "headcount": assembly.headcount}


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def readings(db: Session, assembly: Assembly) -> list[Reading]:
    """Every reading of the session, in time order."""
    entries = db.scalars(
        select(AuditEntry)
        .where(AuditEntry.assembly_id == assembly.id, AuditEntry.action.in_(EVENTS))
        .order_by(AuditEntry.at, AuditEntry.id)
    )
    return [
        Reading(
            at=entry.at,
            event=EVENTS[entry.action],
            headcount=_count(entry.details.get("headcount")),
            attendees=_count(entry.details.get("attendees")),
            poll_id=_count(entry.details.get("poll_id")),
        )
        for entry in entries
    ]


def timeline(db: Session, owner: User, public_id: str) -> list[Reading]:
    """The readings of a session of the account (FR-07), in time order."""
    return readings(db, assemblies.get_assembly(db, owner, public_id))


def worth_reporting(found: list[Reading]) -> bool:
    """Whether readings say more than "the session opened": a headcount, or a later event."""
    return any(
        reading.headcount is not None or reading.event != "session_opened" for reading in found
    )
