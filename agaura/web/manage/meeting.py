"""Tribune and agenda of a session, run from the console (often from a phone).

The tribune form shows one row per person (name, function typed freely), or one empty row when
nobody is there yet; empty rows are ignored. Its "+" button adds a row: ``meeting.js`` adds it
in place, and without JavaScript the button sends the form back to show one more row, values
kept, nothing saved.

The agenda card is one text area, one item per line, edited in place and saved with one button,
then the choice of the item under discussion (a list: none or any item). ``agenda.js`` sends the
text with ``fetch`` (header ``X-Requested-With: fetch``): the answer is the card alone, saved
(200) or refused with its messages on the field (400), and the card follows other consoles
through ``GET /manage/{public_id}/agenda``. The choice of the item is a live form
(``data-live-form``, answered in place by ``run_action``), like "Previous" and "Next" of
Steering.

Without JavaScript, after a validation error or the "+" button, the console page comes back with
the values as typed (status 400 on error) and the messages next to the faulty field or row.
Both forms post to an address ending in ``#tribune`` / ``#agenda``, so the browser scrolls back
to the card.

Every change goes through ``console_write`` and tells the console channel, which the projection
screen and the other consoles follow: the item under discussion shows on the screen at once.

The module also gives the headcount card its attendance record (``_attendance.html``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import zip_longest
from typing import Annotated, Any

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agaura.db import Database
from agaura.exports.attendance import rows as attendance_rows
from agaura.i18n import N_, _
from agaura.models import Assembly, User
from agaura.security import Keyring
from agaura.services import assemblies, exports, meeting
from agaura.services.errors import InvalidInputError, NotFoundError, ServiceError
from agaura.services.meeting import MAX_TRIBUNE_MEMBERS, Member
from agaura.web.deps import CsrfProtected, DatabaseDep, KeyringDep
from agaura.web.manage.common import (
    ACTIONS,
    AgendaSession,
    ConsoleAccess,
    ConsoleUser,
    CurrentAccess,
    OwnedSession,
    console_context,
    console_write,
    from_script,
    router,
    run_action,
)
from agaura.web.manage.session import render_console
from agaura.web.messages import ERRORS, NOTICES, error_message
from agaura.web.realtime import manage_channel
from agaura.web.templating import templates

ERRORS.update(
    {
        "meeting_tribune_too_large": N_("The tribune has at most 20 people."),
        "meeting_name_required": N_("Enter this person's name, or empty the function."),
        "meeting_name_too_long": N_("A name has at most 100 characters."),
        "meeting_role_too_long": N_("A function has at most 100 characters."),
        "meeting_agenda_too_long": N_("The agenda has at most 50 items."),
        "meeting_item_too_long": N_("An agenda item has at most 200 characters."),
        "meeting_no_such_item": N_("This agenda item no longer exists: reload the page."),
    }
)
NOTICES.update(
    {
        "meeting_tribune_saved": N_("Tribune saved."),
        "meeting_agenda_saved": N_("Agenda saved."),
        "meeting_item_changed": N_("Item under discussion changed."),
    }
)
ACTIONS.update(
    {
        "meeting.tribune_set": N_("Tribune edited"),
        "meeting.agenda_set": N_("Agenda edited"),
        "meeting.agenda_item": N_("Item under discussion changed"),
    }
)
ITEM_TOO_LONG = N_("Item %(number)s is longer than 200 characters: shorten it.")

# Numbering or bullets typed at the start of an agenda line: the list is numbered already.
LIST_MARKER = re.compile(r"^\s*(?:[-*\u2022\u2013\u2014]|[0-9]{1,2}[.)])\s+")
POSITION = re.compile(r"[0-9]{1,3}")
MOVES = {"next": 1, "previous": -1}
AGENDA_CARD = "manage/_agenda.html"


# --- Forms --------------------------------------------------------------------------------------


class TribuneFields(BaseModel):
    """The tribune form as typed: rows of ``name`` and ``role`` (function) fields."""

    name: list[str] = []
    role: list[str] = []
    more: str = ""

    def members(self) -> list[Member]:
        pairs = zip_longest(self.name, self.role, fillvalue="")
        return [Member(name, role) for name, role in pairs]


@dataclass(frozen=True, slots=True)
class TribuneForm:
    """What the tribune card shows in its form."""

    rows: list[tuple[str, str]]  # (name, function), members first, then empty rows
    errors: dict[str, str] = field(default_factory=dict)  # "<row>:name" / "<row>:role" → message
    summary: list[str] = field(default_factory=list)  # problems that concern no single row
    open: bool = False  # form unfolded (after an error or the "+" button)
    max_rows: int = MAX_TRIBUNE_MEMBERS

    @property
    def can_add(self) -> bool:
        return len(self.rows) < self.max_rows


@dataclass(frozen=True, slots=True)
class AgendaForm:
    """What the agenda card shows in its text area: the stored agenda, or the text as typed
    after an error, with its messages; ``saved`` for the answer to ``agenda.js``."""

    text: str
    errors: list[str] = field(default_factory=list)
    saved: bool = False


def tribune_rows(members: list[Member], *, empty: int = 0) -> list[tuple[str, str]]:
    """The members, then ``empty`` empty rows, within the size of the tribune; at least one row,
    so that an empty tribune shows one row to fill in."""
    total = max(len(members), min(len(members) + empty, MAX_TRIBUNE_MEMBERS), 1)
    rows = [(member.name, member.role) for member in members]
    return rows + [("", "")] * (total - len(rows))


def tribune_form(
    members: list[Member],
    problems: list[InvalidInputError] | None = None,
    *,
    empty: int = 0,
    open_form: bool = False,
) -> TribuneForm:
    errors: dict[str, str] = {}
    summary: list[str] = []
    for problem in problems or []:
        if problem.detail and problem.detail.startswith("member:"):
            part = "role" if problem.code == "meeting_role_too_long" else "name"
            row = problem.detail.removeprefix("member:")
            errors.setdefault(f"{row}:{part}", error_message(problem))
        else:
            summary.append(error_message(problem))
    return TribuneForm(
        rows=tribune_rows(members, empty=empty),
        errors=errors,
        summary=summary,
        open=open_form or bool(problems),
    )


def agenda_lines(text: str) -> list[str]:
    """One title per line, without the numbering or bullet typed (or pasted) before it."""
    return [LIST_MARKER.sub("", line, count=1) for line in text.splitlines()]


def agenda_messages(problems: list[InvalidInputError]) -> list[str]:
    messages = []
    for problem in problems:
        if problem.code == "meeting_item_too_long" and problem.detail:
            messages.append(_(ITEM_TOO_LONG) % {"number": problem.detail.removeprefix("item:")})
        else:
            messages.append(error_message(problem))
    return messages


def stored_agenda(assembly: Assembly, *, saved: bool = False) -> AgendaForm:
    """The agenda card's text area as stored: one title per line."""
    return AgendaForm(text="\n".join(meeting.agenda_titles(assembly)), saved=saved)


def members_of(assembly: Assembly) -> list[Member]:
    return [
        Member(str(entry.get("name", "")), str(entry.get("role", "")))
        for entry in assembly.tribune or []
    ]


@console_context
def meeting_context(
    request: Request, db: Session, access: ConsoleAccess, assembly: Assembly, keyring: Keyring
) -> dict[str, Any]:
    """The forms of the tribune and agenda cards: as typed after an error, else as stored.
    A delegate never gets the tribune (names are the owner's to manage)."""
    agenda = getattr(request.state, "meeting_agenda", None) or stored_agenda(assembly)
    if not access.is_owner:
        return {"meeting_tribune": None, "meeting_agenda": agenda}
    tribune = getattr(request.state, "meeting_tribune", None)
    if tribune is None:
        tribune = tribune_form(members_of(assembly))
    return {"meeting_tribune": tribune, "meeting_agenda": agenda}


@console_context
def attendance_context(
    request: Request, db: Session, access: ConsoleAccess, assembly: Assembly, keyring: Keyring
) -> dict[str, Any]:
    """Attendance record of the headcount card (FR-28), most recent reading first; the
    owner's only: a delegate with the headcount right sees the card without it."""
    if not access.is_owner:
        return {"meeting_attendance": []}
    numbers = exports.poll_numbers(db, assembly)
    rows = attendance_rows(exports.record_readings(db, assembly, numbers), numbers)
    return {"meeting_attendance": rows[::-1]}


def _console_again(
    request: Request,
    access: ConsoleAccess,
    database: Database,
    keyring: Keyring,
    *,
    tribune: TribuneForm | None = None,
    agenda: AgendaForm | None = None,
    status_code: int,
) -> Response:
    """The console page with a form as typed (``meeting_context`` picks it up)."""
    request.state.meeting_tribune = tribune
    request.state.meeting_agenda = agenda
    return render_console(
        request, access, database, keyring, request.app.state.limiter, status_code=status_code
    )


# --- Routes -------------------------------------------------------------------------------------


@router.post("/manage/{public_id}/tribune", dependencies=[CsrfProtected])
def save_tribune(
    request: Request,
    public_id: OwnedSession,
    access: CurrentAccess,
    user: ConsoleUser,
    database: DatabaseDep,
    keyring: KeyringDep,
    fields: Annotated[TribuneFields, Form()],
) -> Response:
    """Replace the tribune; allowed in every state, to fix a name for the minutes.

    With ``more`` (the "+" button without JavaScript), nothing is saved: the form comes back
    as typed, with one more empty row.
    """
    typed = fields.members()
    kept = meeting.filled_members(typed)
    shown_empty = len(typed) - len(kept)
    if fields.more:
        form = tribune_form(kept, empty=shown_empty + 1, open_form=True)
        return _console_again(request, access, database, keyring, tribune=form, status_code=200)
    if problems := meeting.tribune_problems(typed):
        form = tribune_form(kept, problems, empty=shown_empty)
        return _console_again(request, access, database, keyring, tribune=form, status_code=400)
    return run_action(
        request,
        public_id,
        lambda db: meeting.set_tribune(db, user, public_id, kept),
        notice="meeting_tribune_saved",
        anchor="#tribune",
    )


def _agenda_card(
    request: Request,
    user: User,
    public_id: str,
    form: AgendaForm | None = None,
    *,
    saved: bool = False,
    status_code: int = 200,
) -> Response:
    """The agenda card alone, for ``agenda.js``: ``form`` as typed, else as stored (``saved``:
    just saved). Not ``render()``: it would consume the flash notice of the console."""
    with request.app.state.database.read() as db:
        assembly = assemblies.get_assembly(db, user, public_id)
        context = {
            "assembly": assembly,
            "csrf_token": request.state.csrf_token,
            "meeting_agenda": form or stored_agenda(assembly, saved=saved),
            "agenda_alone": True,
        }
        response = templates.TemplateResponse(
            request, AGENDA_CARD, context, status_code=status_code
        )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/manage/{public_id}/agenda", response_class=HTMLResponse)
def agenda_card(request: Request, public_id: AgendaSession, user: ConsoleUser) -> Response:
    """The card alone, for ``agenda.js`` (refresh after another console's change)."""
    return _agenda_card(request, user, public_id)


@router.post("/manage/{public_id}/agenda", dependencies=[CsrfProtected])
def save_agenda(
    request: Request,
    public_id: AgendaSession,
    access: CurrentAccess,
    user: ConsoleUser,
    database: DatabaseDep,
    keyring: KeyringDep,
    items: Annotated[str, Form()] = "",
) -> Response:
    """Replace the agenda (one item per line); refused once the session is closed.

    From ``agenda.js``, the answer is the card alone: the agenda as saved (numbering and blank
    lines removed), or the text as typed with its messages (status 400). Otherwise the usual
    redirect, or the console page with the text as typed (status 400).
    """
    titles = agenda_lines(items)
    script = from_script(request)
    if problems := meeting.agenda_problems(titles):
        form = AgendaForm(text=items, errors=agenda_messages(problems))
        if script:
            return _agenda_card(request, user, public_id, form, status_code=400)
        return _console_again(request, access, database, keyring, agenda=form, status_code=400)

    def action(db: Session) -> None:
        meeting.set_agenda(db, user, public_id, titles)

    if not script:
        return run_action(
            request, public_id, action, notice="meeting_agenda_saved", anchor="#agenda"
        )
    try:
        with console_write(request) as db:
            action(db)
    except NotFoundError:
        raise
    except ServiceError as error:
        form = AgendaForm(text=items, errors=[error_message(error)])
        return _agenda_card(request, user, public_id, form, status_code=400)
    request.app.state.hub.publish(manage_channel(public_id), "turnout", "console")
    return _agenda_card(request, user, public_id, saved=True)


def _position(raw: str) -> int | None:
    """A position typed in a form: ``none`` (or empty) for none, else 1, 2…"""
    if raw in ("", "none"):
        return None
    if POSITION.fullmatch(raw):
        return int(raw)
    raise InvalidInputError("meeting_no_such_item")


@router.post("/manage/{public_id}/agenda/current", dependencies=[CsrfProtected])
def change_current_item(
    request: Request,
    public_id: AgendaSession,
    user: ConsoleUser,
    item: Annotated[str, Form()] = "",
    seen: Annotated[str, Form()] = "",
) -> Response:
    """Live control: ``item`` is ``next``, ``previous``, ``none`` or a position (1, 2…).

    ``seen`` is the item under discussion when the page was shown: a move from another item
    (double tap, stale tab) changes nothing; a position chosen in the agenda card needs none.
    manage.js sends both forms with fetch: answered in place (``run_action``'s ``in_place``).
    """

    def action(db: Session) -> None:
        if item in MOVES:
            meeting.move_current_item(db, user, public_id, MOVES[item], seen=_position(seen))
        elif item:
            meeting.set_current_item(db, user, public_id, _position(item))
        else:
            raise InvalidInputError("meeting_no_such_item")

    # No anchor: the agenda card and the cockpit each post to the address to come back to. Both
    # are live forms (manage.js): "Previous" and "Next" of Steering, the card's choice of item.
    return run_action(request, public_id, action, notice="meeting_item_changed", in_place=True)
