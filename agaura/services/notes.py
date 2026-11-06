"""Session notes: a timestamped feed written by the tribune during the assembly (minutes).

Each note is plain text (line breaks kept) with the time it was written and the agenda item
under discussion at that moment; that item can be changed afterwards. An **internal** note
stays in the console: it never reaches the minutes or the exports. Notes are never shown to
voters, on the projection screen or on the public results page.

Notes can be written and corrected in every state of the session, closed included, to correct
the minutes afterwards. Once the session is closed, each change is recorded in the session log
(``note.added``, ``note.edited``, ``note.deleted`` with the note's id), so that a correction of
the record after the meeting leaves a trace. The log never holds the text of a note.

Every function takes the current ``owner`` and only reaches that account's sessions (FR-07).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agaura.models import Assembly, AssemblyStatus, Note, User, utcnow
from agaura.services import assemblies, audit, meeting
from agaura.services.errors import InvalidInputError, NotFoundError

MAX_NOTE_LENGTH = 5000
MAX_NOTES = 500


def clean_text(raw: str) -> str:
    """Plain text with ``\\n`` line breaks, trimmed; required, at most ``MAX_NOTE_LENGTH``."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise InvalidInputError("notes_text_required")
    if len(text) > MAX_NOTE_LENGTH:
        raise InvalidInputError("notes_text_too_long")
    return text


def _log_if_closed(
    db: Session, owner: User, assembly: Assembly, action: str, note: Note, now: dt.datetime
) -> None:
    if assembly.status is AssemblyStatus.CLOSED:
        audit.record(
            db,
            action,
            user_id=owner.id,
            assembly_id=assembly.id,
            details={"note_id": note.id},  # never the text
            now=now,
        )


def list_notes(db: Session, owner: User, public_id: str) -> list[Note]:
    """Every note of the session, internal ones included, in time order."""
    assembly = assemblies.get_assembly(db, owner, public_id)
    return session_notes(db, assembly)


def session_notes(db: Session, assembly: Assembly, *, internal: bool = True) -> list[Note]:
    """The notes of a session already reached through its owner, in time order; without the
    internal ones when ``internal`` is false (minutes, exports)."""
    query = select(Note).where(Note.assembly_id == assembly.id)
    if not internal:
        query = query.where(Note.internal.is_(False))
    return list(db.scalars(query.order_by(Note.created_at, Note.id)))


def get_note(db: Session, owner: User, public_id: str, note_id: int) -> Note:
    """A note of a session of the account, else ``NotFoundError`` (also for another session's)."""
    assembly = assemblies.get_assembly(db, owner, public_id)
    note = db.get(Note, note_id)
    if note is None or note.assembly_id != assembly.id:
        raise NotFoundError("note")
    return note


def add_note(
    db: Session,
    owner: User,
    public_id: str,
    text: str,
    *,
    internal: bool = False,
    now: dt.datetime | None = None,
) -> Note:
    """Add a note, attached to the agenda item under discussion (if any)."""
    now = now or utcnow()
    assembly = assemblies.get_assembly(db, owner, public_id)
    text = clean_text(text)
    count = db.scalar(select(func.count()).select_from(Note).where(Note.assembly_id == assembly.id))
    if (count or 0) >= MAX_NOTES:
        raise InvalidInputError("notes_too_many")
    position, title = meeting.current_item(assembly)
    note = Note(
        assembly_id=assembly.id,
        text=text,
        internal=internal,
        item_position=position,
        item_title=title,
        created_at=now,
    )
    db.add(note)
    db.flush()
    _log_if_closed(db, owner, assembly, "note.added", note, now)
    return note


def update_note(
    db: Session,
    owner: User,
    public_id: str,
    note_id: int,
    text: str,
    *,
    internal: bool,
    item: int | None,
    now: dt.datetime | None = None,
) -> Note:
    """Correct a note: its text, whether it is internal, and its agenda item (a position in
    the current agenda, or None for none)."""
    now = now or utcnow()
    note = get_note(db, owner, public_id, note_id)
    assembly = note.assembly
    text = clean_text(text)
    titles = meeting.agenda_titles(assembly)
    if item is not None and not 1 <= item <= len(titles):
        raise InvalidInputError("meeting_no_such_item")
    note.text = text
    note.internal = internal
    note.item_position = item
    note.item_title = None if item is None else titles[item - 1]
    note.updated_at = now
    _log_if_closed(db, owner, assembly, "note.edited", note, now)
    return note


def delete_note(
    db: Session, owner: User, public_id: str, note_id: int, *, now: dt.datetime | None = None
) -> None:
    note = get_note(db, owner, public_id, note_id)
    _log_if_closed(db, owner, note.assembly, "note.deleted", note, now or utcnow())
    db.delete(note)
    db.flush()


def note_item(note: Note, titles: list[str]) -> int | None:
    """The position of the note's agenda item in the agenda ``titles`` as it is now, or None."""
    return meeting.locate_item(titles, note.item_position, note.item_title)
