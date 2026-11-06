"""The tribune and the agenda of a session (who runs it, what it deals with).

The **tribune** lists the people running the session, each with a function typed freely
("Présidente de séance", "Secrétaire"…): there is no predefined list. Its names appear in the
minutes, so it stays editable in every state of the session, closed included, to fix a name
afterwards.

The **agenda** lists the items of the session in order; ``agenda_current`` is the position
(1, 2, …) of the item under discussion, highlighted on the projection screen. It can be changed
until the session is closed. An edit replaces the whole list (the console types it as one text,
one item per line); the item under discussion follows its title, else its position
(``locate_item``), the rule that also places notes and polls after an edit.

Every function takes the current ``owner`` and only reaches that account's sessions (FR-07).
Audit entries record what changed (a count, a position), never the names of the tribune.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from agaura.models import Assembly, AssemblyStatus, AuditEntry, User
from agaura.services import assemblies, audit
from agaura.services.errors import InvalidInputError

MAX_TRIBUNE_MEMBERS = 20
MAX_NAME_LENGTH = 100
MAX_ROLE_LENGTH = 100
MAX_AGENDA_ITEMS = 50
MAX_ITEM_LENGTH = 200


@dataclass(frozen=True, slots=True)
class Member:
    """One person of the tribune, as typed: ``role`` is the function, free text, maybe empty."""

    name: str
    role: str = ""


# --- Tribune ------------------------------------------------------------------------------------


def filled_members(members: Iterable[Member]) -> list[Member]:
    """The members with a name or a function, trimmed; rows with neither are ignored."""
    trimmed = (Member(member.name.strip(), member.role.strip()) for member in members)
    return [member for member in trimmed if member.name or member.role]


def tribune_problems(members: Iterable[Member]) -> list[InvalidInputError]:
    """Every problem of a tribune, each with ``detail`` ``"member:<n>"`` for a faulty row.

    ``n`` counts from 1 among the rows kept by ``filled_members`` (empty rows are skipped), so
    a form that shows the kept rows first can put each message next to its row.
    """
    kept = filled_members(members)
    problems: list[InvalidInputError] = []
    if len(kept) > MAX_TRIBUNE_MEMBERS:
        problems.append(InvalidInputError("meeting_tribune_too_large"))
    for number, member in enumerate(kept, start=1):
        row = f"member:{number}"
        if not member.name:
            problems.append(InvalidInputError("meeting_name_required", row))
        elif len(member.name) > MAX_NAME_LENGTH:
            problems.append(InvalidInputError("meeting_name_too_long", row))
        if len(member.role) > MAX_ROLE_LENGTH:
            problems.append(InvalidInputError("meeting_role_too_long", row))
    return problems


def clean_tribune(members: Iterable[Member]) -> list[dict[str, str]]:
    """The tribune as stored (``[{"name", "role"}, …]``), or the first problem raised."""
    members = list(members)
    if problems := tribune_problems(members):
        raise problems[0]
    return [{"name": member.name, "role": member.role} for member in filled_members(members)]


def set_tribune(
    db: Session,
    owner: User,
    public_id: str,
    members: Iterable[Member],
    *,
    now: dt.datetime | None = None,
) -> Assembly:
    """Replace the tribune, in the order given. Allowed in every state of the session."""
    assembly = assemblies.get_assembly(db, owner, public_id)
    tribune = clean_tribune(members)
    assembly.tribune = tribune  # a new list: the JSON column sees the change
    audit.record(
        db,
        "meeting.tribune_set",
        user_id=owner.id,
        assembly_id=assembly.id,
        details={"count": len(tribune)},  # never the names
        now=now,
    )
    return assembly


# --- Agenda -------------------------------------------------------------------------------------


def filled_items(titles: Iterable[str]) -> list[str]:
    """The titles, trimmed; blank ones are ignored."""
    return [title for title in (raw.strip() for raw in titles) if title]


def agenda_problems(titles: Iterable[str]) -> list[InvalidInputError]:
    """Every problem of an agenda; ``detail`` ``"item:<n>"`` names a faulty item (from 1, blank
    titles skipped)."""
    kept = filled_items(titles)
    problems: list[InvalidInputError] = []
    if len(kept) > MAX_AGENDA_ITEMS:
        problems.append(InvalidInputError("meeting_agenda_too_long"))
    problems.extend(
        InvalidInputError("meeting_item_too_long", f"item:{number}")
        for number, title in enumerate(kept, start=1)
        if len(title) > MAX_ITEM_LENGTH
    )
    return problems


def clean_agenda(titles: Iterable[str]) -> list[str]:
    titles = list(titles)
    if problems := agenda_problems(titles):
        raise problems[0]
    return filled_items(titles)


def locate_item(titles: Sequence[str], position: int | None, title: str | None) -> int | None:
    """Where an item recorded as (``position``, ``title``) is in the agenda ``titles`` now.

    The same title, if it is still there (the occurrence nearest to the recorded position), so
    that inserting or removing other items does not move it; otherwise the same position if it
    still exists (a title corrected in place); otherwise none. Notes and polls keep the item
    under discussion when they were written or pushed, and are placed with this rule.
    """
    if title is not None:
        places = [number for number, item in enumerate(titles, start=1) if item == title]
        if places:
            return min(places, key=lambda number: abs(number - (position or 0)))
    if position is not None and 1 <= position <= len(titles):
        return position
    return None


def _follow_current(old: Sequence[str], current: int | None, new: Sequence[str]) -> int | None:
    """Where the item under discussion is after an edit of the agenda (``locate_item``)."""
    if current is None:
        return None
    title = old[current - 1] if 1 <= current <= len(old) else None
    return locate_item(new, current, title)


def current_item(assembly: Assembly) -> tuple[int | None, str | None]:
    """The item under discussion, as (position, title), or (None, None)."""
    titles = agenda_titles(assembly)
    position = assembly.agenda_current
    if position is None or not 1 <= position <= len(titles):
        return None, None
    return position, titles[position - 1]


def poll_items(db: Session, assembly: Assembly) -> dict[int, tuple[int | None, str | None]]:
    """Poll id → the agenda item under discussion when the poll was pushed, as recorded in the
    details of its ``poll.opened`` audit entry (polls pushed before that was recorded: none)."""
    entries = db.scalars(
        select(AuditEntry).where(
            AuditEntry.assembly_id == assembly.id, AuditEntry.action == "poll.opened"
        )
    )
    found: dict[int, tuple[int | None, str | None]] = {}
    for entry in entries:
        poll_id, position, title = (
            entry.details.get(key) for key in ("poll_id", "agenda_position", "agenda_title")
        )
        if isinstance(poll_id, int):
            found[poll_id] = (
                position if isinstance(position, int) else None,
                title if isinstance(title, str) else None,
            )
    return found


def _open(db: Session, owner: User, public_id: str) -> Assembly:
    """A session of the account whose agenda can still change: not closed."""
    assembly = assemblies.get_assembly(db, owner, public_id)
    assemblies.require_status(assembly, AssemblyStatus.PREPARATION, AssemblyStatus.IN_SESSION)
    return assembly


def agenda_titles(assembly: Assembly) -> list[str]:
    return [str(item.get("title", "")) for item in assembly.agenda or []]


def set_agenda(
    db: Session,
    owner: User,
    public_id: str,
    titles: Iterable[str],
    *,
    now: dt.datetime | None = None,
) -> Assembly:
    """Replace the agenda (blank titles ignored); refused once the session is closed.

    The item under discussion keeps pointing at a valid position, or at none (see
    ``_follow_current``). Saving the agenda again unchanged changes nothing and logs nothing
    (the card is edited in place: its button may be pressed with nothing to save).
    """
    assembly = _open(db, owner, public_id)
    new = clean_agenda(titles)
    old = agenda_titles(assembly)
    current = _follow_current(old, assembly.agenda_current, new)
    if new == old and current == assembly.agenda_current:
        return assembly
    assembly.agenda_current = current
    assembly.agenda = [{"title": title} for title in new]
    audit.record(
        db,
        "meeting.agenda_set",
        user_id=owner.id,
        assembly_id=assembly.id,
        details={"count": len(new)},
        now=now,
    )
    return assembly


def set_current_item(
    db: Session,
    owner: User,
    public_id: str,
    position: int | None,
    *,
    now: dt.datetime | None = None,
) -> Assembly:
    """Highlight the item at ``position`` (from 1), or none; refused once closed.

    Choosing the item already highlighted changes nothing and logs nothing (a double tap).
    """
    assembly = _open(db, owner, public_id)
    if position is not None and not 1 <= position <= len(assembly.agenda or []):
        raise InvalidInputError("meeting_no_such_item")
    if position == assembly.agenda_current:
        return assembly
    assembly.agenda_current = position
    audit.record(
        db,
        "meeting.agenda_item",
        user_id=owner.id,
        assembly_id=assembly.id,
        details={"position": position},
        now=now,
    )
    return assembly


def move_current_item(
    db: Session,
    owner: User,
    public_id: str,
    step: int,
    *,
    seen: int | None,
    now: dt.datetime | None = None,
) -> Assembly:
    """Go to the next (``step=1``) or previous (``step=-1``) item.

    ``seen`` is the item the caller saw under discussion when asking. If it is no longer the
    one stored (a double tap on "next", another console tab), nothing changes: a double tap
    must never skip an item. With no item under discussion, the next one is the first; moving
    past either end raises ``meeting_no_such_item``, like a position that does not exist.
    """
    assembly = _open(db, owner, public_id)
    if assembly.agenda_current != seen:
        return assembly
    return set_current_item(db, owner, public_id, (seen or 0) + step, now=now)
