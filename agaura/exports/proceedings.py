"""Course of the session for the minutes ("Déroulé de la séance", FR-62): the tribune's notes
and the polls pushed, grouped by agenda item, each group in time order.

A note belongs to the item it was attached to (under discussion when it was written, or chosen
later); a poll to the item under discussion when it was pushed. Both are placed in the agenda
as it is now (``meeting.locate_item``). Entries outside any item come last; without an agenda,
the course is one chronological feed. Internal notes never get here (``SessionRecord.notes``).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from agaura.models import Note
from agaura.services.exports import PollRecord, SessionRecord
from agaura.web.templating import DISPLAY_TIMEZONE, short_datetime, time_of_day

_EPOCH = dt.datetime.min.replace(tzinfo=dt.UTC)


@dataclass(frozen=True, slots=True)
class Entry:
    at: dt.datetime
    when: str  # "14:05", or with the date when the session spans several days
    note: Note | None = None
    record: PollRecord | None = None


@dataclass(frozen=True, slots=True)
class Group:
    position: int | None  # agenda item, None: outside the agenda (or no agenda at all)
    title: str | None
    entries: list[Entry] = field(default_factory=list)


def worth_showing(session: SessionRecord) -> bool:
    """A note to report, or polls to place in an agenda; otherwise the section would only
    repeat the list of polls."""
    return bool(session.notes) or bool(session.agenda and session.polls)


def groups(session: SessionRecord) -> list[Group]:
    titles = session.agenda
    found: list[tuple[int | None, dt.datetime, int, Note | None, PollRecord | None]] = []
    for note in session.notes:
        found.append((session.note_item(note), note.created_at, 0, note, None))
    for record in session.polls:
        at = record.poll.opened_at or _EPOCH
        found.append((session.poll_item(record.poll.id), at, 1, None, record))
    days = {at.astimezone(DISPLAY_TIMEZONE).date() for _, at, _, _, _ in found if at != _EPOCH}
    when = time_of_day if len(days) <= 1 else short_datetime

    def entries(position: int | None) -> list[Entry]:
        chosen = sorted(
            (item for item in found if item[0] == position or not titles),
            key=lambda item: (item[1], item[2]),
        )
        return [Entry(at, when(at), note, record) for _, at, _, note, record in chosen]

    if not titles:
        return [Group(None, None, entries(None))] if found else []
    result = [Group(number, title, entries(number)) for number, title in enumerate(titles, 1)]
    outside = entries(None)
    if outside:
        result.append(Group(None, None, outside))
    return result
