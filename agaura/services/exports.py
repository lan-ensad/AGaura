"""Data behind the exports and the public results page (FR-17, FR-51, FR-60 to FR-63).

Admin functions take the current ``owner`` and only reach that account's sessions (FR-07). They
load everything an export needs (options, stored result, ballots) inside the caller's read
transaction, so the documents can be built and rendered after it has ended.

Secrecy (SPEC §9.2): the ballots of a poll are only handed out once voting on it has stopped
(closed, published or cancelled). Exporting an open poll repeatedly would let an observer
match each new ballot with the person who just voted.

A trial vote (``Poll.trial``, D-I) is no part of the record: it gets no number, and the minutes,
the session export, the public results and the attendance readings they carry leave it out.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from agaura.models import Assembly, Ballot, Note, Participation, Poll, PollStatus, User
from agaura.services import access, assemblies, attendance, audit, meeting, notes, polls
from agaura.services.attendance import Reading
from agaura.services.errors import InvalidStateError, NotFoundError

# Polls whose ballots and result can be exported: voting on them has stopped.
FINISHED = (PollStatus.CLOSED, PollStatus.PUBLISHED, PollStatus.CANCELLED)
# Polls shown by the public results link: results shown at least once, and cancellations.
PUBLIC = (PollStatus.PUBLISHED, PollStatus.CANCELLED)

_EPOCH = dt.datetime.min.replace(tzinfo=dt.UTC)


@dataclass(frozen=True, slots=True)
class PollRecord:
    poll: Poll  # options and result loaded, readable after the transaction
    number: int  # rank of the poll in the session order (1, 2, 3…), as in the minutes
    turnout: int  # participations, i.e. ballots cast
    ballots: tuple[Ballot, ...] | None  # sorted by id; None when not requested


@dataclass(frozen=True, slots=True)
class SessionRecord:
    assembly: Assembly
    attendees: int
    quorum: int | None  # attendees required, None when unknown
    polls: list[PollRecord]  # every poll that left the draft state, in session order
    attendance: list[Reading]  # attendance readings, in time order (FR-28)
    notes: list[Note]  # session notes for the minutes, in time order; never the internal ones
    # Poll id → agenda item under discussion when it was pushed, as recorded (position, title).
    poll_items: dict[int, tuple[int | None, str | None]]
    # Anchor of the session's last log entry (``audit.session_anchor``), for the minutes only.
    log_anchor: str | None = None

    @property
    def quorum_reached(self) -> bool | None:
        return None if self.quorum is None else self.attendees >= self.quorum

    @property
    def finished(self) -> list[PollRecord]:
        return [record for record in self.polls if record.poll.status in FINISHED]

    @property
    def numbers(self) -> dict[int, int]:
        """Poll id → number in the session order."""
        return {record.poll.id: record.number for record in self.polls}

    @property
    def agenda(self) -> list[str]:
        return meeting.agenda_titles(self.assembly)

    def note_item(self, note: Note) -> int | None:
        """Position, in the agenda as it is now, of the item a note belongs to."""
        return meeting.locate_item(self.agenda, note.item_position, note.item_title)

    def poll_item(self, poll_id: int) -> int | None:
        """Position, in the agenda as it is now, of the item under discussion when the poll was
        pushed."""
        return meeting.locate_item(self.agenda, *self.poll_items.get(poll_id, (None, None)))


def session_order(poll: Poll) -> tuple[dt.datetime, int]:
    """Polls are numbered in the order they were pushed, as in the console."""
    return (poll.opened_at or _EPOCH, poll.id)


def poll_numbers(db: Session, assembly: Assembly) -> dict[int, int]:
    """Poll id → number in the session order, for every poll pushed so far, trial votes
    aside."""
    rows = db.execute(
        select(Poll.id, Poll.opened_at).where(
            Poll.assembly_id == assembly.id,
            Poll.status != PollStatus.DRAFT,
            Poll.trial.is_(False),
        )
    ).all()
    ordered = sorted(rows, key=lambda row: (row.opened_at or _EPOCH, row.id))
    return {row.id: number for number, row in enumerate(ordered, start=1)}


def record_readings(db: Session, assembly: Assembly, numbers: dict[int, int]) -> list[Reading]:
    """The attendance readings of the record (FR-28), without those of a trial vote: a poll
    reading counts only for a poll with a number."""
    return [
        reading
        for reading in attendance.readings(db, assembly)
        if reading.poll_id is None or reading.poll_id in numbers
    ]


def _turnouts(db: Session, poll_ids: list[int]) -> dict[int, int]:
    if not poll_ids:
        return {}
    rows = db.execute(
        select(Participation.poll_id, func.count(Participation.credential_id))
        .where(Participation.poll_id.in_(poll_ids))
        .group_by(Participation.poll_id)
    )
    return {poll_id: count for poll_id, count in rows}


def _ballots(db: Session, poll_ids: list[int]) -> dict[int, tuple[Ballot, ...]]:
    found: dict[int, list[Ballot]] = {poll_id: [] for poll_id in poll_ids}
    if poll_ids:
        for ballot in db.scalars(
            select(Ballot).where(Ballot.poll_id.in_(poll_ids)).order_by(Ballot.id)
        ):
            found[ballot.poll_id].append(ballot)
    return {poll_id: tuple(rows) for poll_id, rows in found.items()}


def _pushed_polls(db: Session, assembly: Assembly) -> list[Poll]:
    """Every poll pushed so far, in session order, trial votes aside."""
    rows = db.scalars(
        select(Poll)
        .options(selectinload(Poll.options), selectinload(Poll.result))
        .where(
            Poll.assembly_id == assembly.id,
            Poll.status != PollStatus.DRAFT,
            Poll.trial.is_(False),
        )
    ).all()
    return sorted(rows, key=session_order)


def _records(
    db: Session, rows: list[Poll], *, numbers: dict[int, int], with_ballots: bool
) -> list[PollRecord]:
    ids = [poll.id for poll in rows]
    turnouts = _turnouts(db, ids)
    # Never hand out the ballots of a poll that is still open (see the module docstring).
    ballots = _ballots(db, [poll.id for poll in rows if poll.status in FINISHED])
    return [
        PollRecord(
            poll=poll,
            number=numbers[poll.id],
            turnout=turnouts.get(poll.id, 0),
            ballots=ballots.get(poll.id) if with_ballots else None,
        )
        for poll in rows
    ]


def session_record(
    db: Session, owner: User, public_id: str, *, with_ballots: bool = False
) -> SessionRecord:
    """The session and every poll pushed so far, for the session exports and the minutes; a
    trial vote is left out, with the attendance readings of its opening and closing."""
    assembly = assemblies.get_assembly(db, owner, public_id)
    rows = _pushed_polls(db, assembly)
    numbers = {poll.id: number for number, poll in enumerate(rows, start=1)}
    return SessionRecord(
        assembly=assembly,
        attendees=access.attendees(db, assembly),
        quorum=assemblies.quorum_required(assembly),
        polls=_records(db, rows, numbers=numbers, with_ballots=with_ballots),
        attendance=record_readings(db, assembly, numbers),
        notes=notes.session_notes(db, assembly, internal=False),
        poll_items=meeting.poll_items(db, assembly),
        log_anchor=audit.session_anchor(db, assembly.id),
    )


def poll_record(
    db: Session, owner: User, public_id: str, poll_id: int
) -> tuple[SessionRecord, PollRecord]:
    """One finished poll of the session in the URL, with its ballots, for the poll exports.

    Raises ``NotFoundError`` for a poll of another session or account,
    ``InvalidStateError("export_poll_not_finished")`` while the poll is a draft or open, and
    ``InvalidStateError("export_trial_poll")`` for a trial vote, which no export carries.
    """
    poll = polls.get_poll(db, owner, poll_id)
    if poll.assembly.public_id != public_id:
        raise NotFoundError("poll")
    if poll.status not in FINISHED:
        raise InvalidStateError("export_poll_not_finished")
    if poll.trial:
        raise InvalidStateError("export_trial_poll")
    session = session_record(db, owner, public_id)
    number = next(record.number for record in session.polls if record.poll.id == poll_id)
    (record,) = _records(db, [poll], numbers={poll.id: number}, with_ballots=True)
    return session, record


def record_export(
    db: Session,
    owner: User,
    public_id: str,
    action: str,
    *,
    poll: Poll | None = None,
    now: dt.datetime | None = None,
) -> None:
    """Log a download in the session log (FR-80): what was exported, never its content."""
    assembly = assemblies.get_assembly(db, owner, public_id)
    details = {} if poll is None else {"poll_id": poll.id, "title": poll.title}
    audit.record(db, action, user_id=owner.id, assembly_id=assembly.id, details=details, now=now)


# --- Public results link (FR-17) --------------------------------------------------------------


def public_results(db: Session, assembly: Assembly) -> list[Poll]:
    """Polls of the session behind a valid public link: published results and cancellations.

    Drafts, open polls, closed polls whose result was never published and trial votes stay
    hidden.
    """
    return [poll for poll in _pushed_polls(db, assembly) if poll.status in PUBLIC]
