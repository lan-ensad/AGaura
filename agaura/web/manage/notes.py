"""Session notes taken during the assembly, for the minutes (console card "Notes de séance").

A note is added with its time and the agenda item under discussion; "internal" keeps it out of
the minutes and the exports. Note identifiers travel in form fields (``note``), never in paths.

The card has its own rubric, Session notes; during the session Steering also has its "Quick note"
command (D-A). Without JavaScript the forms follow POST/redirect/GET through ``run_action``: an
added note comes back to the copy it was typed in (``#notes``, ``#cmd-notes``), a corrected one
to its row. ``notes.js`` sends them with ``fetch`` (header ``X-Requested-With: fetch``) instead:
the answer is then both copies (``_notes_alone.html``), or the error message with status 400,
so a note is added without reloading the page; the console channel is told either way, and
other consoles refetch them (``GET /manage/{public_id}/notes``) when their live panel refreshes.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from sqlalchemy.orm import Session

from agaura.i18n import N_
from agaura.models import Assembly, Note, User, utcnow
from agaura.security import Keyring
from agaura.services import assemblies, meeting, notes
from agaura.services.delegates import Right
from agaura.services.errors import InvalidInputError, NotFoundError, ServiceError
from agaura.services.notes import MAX_NOTE_LENGTH
from agaura.web.deps import CsrfProtected
from agaura.web.manage.common import (
    ACTIONS,
    ConsoleAccess,
    ConsoleUser,
    NotesSession,
    console_context,
    console_write,
    router,
    run_action,
)
from agaura.web.messages import ERRORS, NOTICES, error_message
from agaura.web.realtime import manage_channel
from agaura.web.templating import DISPLAY_TIMEZONE, short_datetime, templates, time_of_day

ERRORS.update(
    {
        "notes_text_required": N_("Write the note before adding it."),
        "notes_text_too_long": N_("A note has at most 5,000 characters."),
        "notes_too_many": N_("This session has reached the maximum of 500 notes."),
    }
)
NOTICES.update(
    {
        "notes_added": N_("Note added."),
        "notes_saved": N_("Note saved."),
        "notes_deleted": N_("Note deleted."),
    }
)
ACTIONS.update(
    {
        "note.added": N_("Note added after the session closed"),
        "note.edited": N_("Note edited after the session closed"),
        "note.deleted": N_("Note deleted after the session closed"),
    }
)
NOTE_ID = re.compile(r"[0-9]{1,9}")
POSITION = re.compile(r"[0-9]{1,3}")
TEMPLATE = "manage/_notes_alone.html"  # the card, and the command during the session


@dataclass(frozen=True, slots=True)
class NoteView:
    id: int
    text: str
    internal: bool
    at: dt.datetime
    when: str  # "14:05" today, else with the date
    edited: bool
    item: int | None  # position in the agenda as it is now
    item_title: str | None


def _when(at: dt.datetime, today: dt.date) -> str:
    return (
        time_of_day(at) if at.astimezone(DISPLAY_TIMEZONE).date() == today else short_datetime(at)
    )


def card_context(db: Session, assembly: Assembly) -> dict[str, Any]:
    """Variables of ``_notes.html``: the notes (most recent first), the item under discussion
    and the agenda (for the edit forms)."""
    titles = meeting.agenda_titles(assembly)
    today = utcnow().astimezone(DISPLAY_TIMEZONE).date()

    def view(note: Note) -> NoteView:
        item = notes.note_item(note, titles)
        return NoteView(
            id=note.id,
            text=note.text,
            internal=note.internal,
            at=note.created_at,
            when=_when(note.created_at, today),
            edited=note.updated_at is not None,
            item=item,
            item_title=None if item is None else titles[item - 1],
        )

    found = notes.session_notes(db, assembly)
    return {
        "notes_items": [view(note) for note in reversed(found)],
        "notes_current": meeting.current_item(assembly),
        "notes_agenda": titles,
        "notes_max_length": MAX_NOTE_LENGTH,
    }


@console_context
def notes_context(
    request: Request, db: Session, access: ConsoleAccess, assembly: Assembly, keyring: Keyring
) -> dict[str, Any]:
    """The notes, internal ones included, for the owner and the delegates of the notes."""
    if not access.allows(Right.NOTES):
        return {"notes_items": [], "notes_current": None, "notes_agenda": [], "notes_max_length": 0}
    return card_context(db, assembly)


def _card(request: Request, user: User, public_id: str, *, status_code: int = 200) -> Response:
    """The card alone. Not ``render()``: it would consume the flash notice of the console."""
    with request.app.state.database.read() as db:
        assembly = assemblies.get_assembly(db, user, public_id)
        context = {"assembly": assembly, "csrf_token": request.state.csrf_token}
        context.update(card_context(db, assembly))
        response = templates.TemplateResponse(request, TEMPLATE, context, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def _from_script(request: Request) -> bool:
    return request.headers.get("x-requested-with") == "fetch"


def _note_action(
    request: Request, public_id: str, user: User, action: Any, *, notice: str, anchor: str
) -> Response:
    """``run_action`` for a form; for ``notes.js``, the same write and notification, answered
    with the card (or the error message, status 400) instead of a redirect."""
    if not _from_script(request):
        return run_action(request, public_id, action, notice=notice, anchor=anchor)
    try:
        with console_write(request) as db:
            action(db)
    except NotFoundError:
        raise
    except ServiceError as error:
        return PlainTextResponse(error_message(error), status_code=400)
    request.app.state.hub.publish(manage_channel(public_id), "turnout", "console")
    return _card(request, user, public_id)


def _note_id(raw: str) -> int:
    if NOTE_ID.fullmatch(raw):
        return int(raw)
    raise NotFoundError("note")


def _item(raw: str) -> int | None:
    if not raw:
        return None
    if POSITION.fullmatch(raw):
        return int(raw)
    raise InvalidInputError("meeting_no_such_item")


@router.get("/manage/{public_id}/notes", response_class=HTMLResponse)
def notes_card(request: Request, public_id: NotesSession, user: ConsoleUser) -> Response:
    """The card alone, for ``notes.js`` (refresh after another console's change)."""
    return _card(request, user, public_id)


@router.post("/manage/{public_id}/notes", dependencies=[CsrfProtected])
def add_note(
    request: Request,
    public_id: NotesSession,
    user: ConsoleUser,
    text: Annotated[str, Form()] = "",
    internal: Annotated[str, Form()] = "",
) -> Response:
    return _note_action(
        request,
        public_id,
        user,
        lambda db: notes.add_note(db, user, public_id, text, internal=internal == "1"),
        notice="notes_added",
        anchor="",  # the card (#notes) and the command (#cmd-notes) post to their own address
    )


@router.post("/manage/{public_id}/notes/edit", dependencies=[CsrfProtected])
def edit_note(
    request: Request,
    public_id: NotesSession,
    user: ConsoleUser,
    note: Annotated[str, Form()] = "",
    text: Annotated[str, Form()] = "",
    internal: Annotated[str, Form()] = "",
    item: Annotated[str, Form()] = "",
) -> Response:
    """Correct a note (text, internal, agenda item); allowed in every state of the session.
    Another session's note answers 404, like a missing one."""
    note_id = _note_id(note)

    def action(db: Session) -> None:
        notes.update_note(
            db, user, public_id, note_id, text, internal=internal == "1", item=_item(item)
        )

    return _note_action(
        request, public_id, user, action, notice="notes_saved", anchor=f"#note-{note_id}"
    )


@router.post("/manage/{public_id}/notes/delete", dependencies=[CsrfProtected])
def delete_note(
    request: Request,
    public_id: NotesSession,
    user: ConsoleUser,
    note: Annotated[str, Form()] = "",
) -> Response:
    note_id = _note_id(note)
    return _note_action(
        request,
        public_id,
        user,
        lambda db: notes.delete_note(db, user, public_id, note_id),
        notice="notes_deleted",
        anchor="#notes",
    )
