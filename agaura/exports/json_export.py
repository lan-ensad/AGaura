"""Complete JSON export of a poll or a session (FR-61, FR-63).

The same document shape serves both: a session export lists every finished poll, a poll export
lists one. Each poll carries its ``definition`` and ``ballots`` exactly in the canonical form of
``agaura.services.results``, so its digest can be recomputed from the file alone; the format is
documented field by field in ``docs/EXPORTS.md``.

Secrecy (SPEC §9.2): ballots are listed by their random id, without any link to a credential
and without any timestamp.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from agaura.services import results
from agaura.services.attendance import Reading
from agaura.services.exports import PollRecord, SessionRecord

FORMAT = "agaura-export"
VERSION = 1


def timestamp(value: dt.datetime | None) -> str | None:
    """UTC, ISO 8601, to the second: ``2026-06-20T12:03:11+00:00``."""
    return None if value is None else value.astimezone(dt.UTC).isoformat(timespec="seconds")


def session_entry(session: SessionRecord) -> dict[str, Any]:
    assembly = session.assembly
    return {
        "id": assembly.public_id,
        "title": assembly.title,
        "date": None if assembly.date is None else assembly.date.isoformat(),
        "place": assembly.place,
        "status": assembly.status.value,
        "access_mode": assembly.access_mode.value,
        "registered_members": assembly.registered_count,
        "quorum_rule": assembly.quorum_rule,
        "quorum_required": session.quorum,
        "attendees": session.attendees,
        "headcount": assembly.headcount,
        "opened_at": timestamp(assembly.opened_at),
        "closed_at": timestamp(assembly.closed_at),
        # Added within version 1: readers ignore the fields they do not know (docs/EXPORTS.md).
        "tribune": [
            {"name": member.get("name"), "role": member.get("role") or None}
            for member in assembly.tribune or []
        ],
        "agenda": [{"title": item.get("title")} for item in assembly.agenda or []],
        "attendance": [
            attendance_entry(reading, session.numbers) for reading in session.attendance
        ],
        # Notes of the tribune, never the internal ones: this file may be shared for a recount.
        "notes": [
            {
                "at": timestamp(note.created_at),
                "updated_at": timestamp(note.updated_at),
                "agenda_item": session.note_item(note),
                "text": note.text,
            }
            for note in session.notes
        ],
    }


def attendance_entry(reading: Reading, numbers: dict[int, int]) -> dict[str, Any]:
    """One attendance reading (FR-28): aggregated counts, never anything about one voter."""
    return {
        "at": timestamp(reading.at),
        "event": reading.event,
        "poll_number": None if reading.poll_id is None else numbers.get(reading.poll_id),
        "headcount": reading.headcount,
        "attendees": reading.attendees,
    }


def poll_entry(record: PollRecord, *, agenda_item: int | None = None) -> dict[str, Any]:
    poll = record.poll
    result = poll.result
    return {
        "number": record.number,
        "id": poll.id,
        "status": poll.status.value,
        "opened_at": timestamp(poll.opened_at),
        "closed_at": timestamp(poll.closed_at),
        "cancel_reason": poll.cancel_reason,
        "turnout": record.turnout,
        "definition": results.canonical_poll(poll),
        "ballots": results.canonical_ballots(record.ballots or ()),
        "result": None
        if result is None
        else {
            "ballots_sha256": result.ballots_sha256,
            "computed_at": timestamp(result.computed_at),
            "published_at": timestamp(result.published_at),
            "data": result.data,
        },
        # Position in ``session.agenda`` of the item under discussion when it was pushed.
        "agenda_item": agenda_item,
    }


def document(
    session: SessionRecord, records: list[PollRecord], *, now: dt.datetime
) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "version": VERSION,
        "generated_at": timestamp(now),
        "session": session_entry(session),
        "polls": [
            poll_entry(record, agenda_item=session.poll_item(record.poll.id)) for record in records
        ],
    }


def dumps(value: dict[str, Any]) -> bytes:
    """Readable UTF-8 JSON; the digests do not depend on this layout (see ``recount``)."""
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
