"""Exports of a session: ballots as CSV, complete JSON, PDF minutes (FR-60 to FR-63).

Owner only: the session comes from ``OwnedSession`` and polls are reached through owner-scoped
services, so another account gets 404. A poll can be exported once voting on it has stopped
(closed, published or cancelled), otherwise the answer is 409, as for a trial vote, which no
export carries (D-I); session exports always work.

Each document is built from a read transaction, the PDF rendered after it, and the download is
then recorded in the session log (FR-80) in a short write transaction: what was exported, never
its content. Responses are attachments that no cache keeps.

Every download of the console sits in Documents (D-H, ``_exports.html``): the session files,
then one row per poll pushed (``exports_polls``), with its links once voting on it has stopped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from agaura.db import Database
from agaura.exports import csv_export, json_export, minutes
from agaura.i18n import N_, _
from agaura.models import Assembly, Poll, User, utcnow
from agaura.security import Keyring
from agaura.services import exports
from agaura.services.errors import InvalidStateError
from agaura.web.deps import DatabaseDep, RowId, SameSiteOnly
from agaura.web.manage.common import (
    ACTIONS,
    ConsoleAccess,
    ConsoleUser,
    OwnedSession,
    console_context,
    pdf_rendering,
    router,
)
from agaura.web.messages import ERRORS, error_message
from agaura.web.templating import render

ERRORS.update(
    {
        "export_poll_not_finished": N_("This poll can be exported once voting on it has stopped."),
        "export_trial_poll": N_(
            "A trial vote only tests the room's network: no export carries it, nor the minutes."
        ),
    }
)
ACTIONS.update(
    {
        "export.ballots_csv": N_("Ballots exported (CSV)"),
        "export.poll_json": N_("Poll exported (JSON)"),
        "export.session_json": N_("Session exported (JSON)"),
        "export.minutes_pdf": N_("Minutes downloaded (PDF)"),
    }
)

# File names, translated like the rest of the interface; keep the translations ASCII.
BALLOTS_CSV_NAME = N_("agaura-%(id)s-poll-%(number)s-ballots.csv")
POLL_JSON_NAME = N_("agaura-%(id)s-poll-%(number)s.json")
SESSION_JSON_NAME = N_("agaura-%(id)s-session.json")
MINUTES_PDF_NAME = N_("agaura-%(id)s-minutes.pdf")

CSV_TYPE = "text/csv; charset=utf-8"
JSON_TYPE = "application/json"
PDF_TYPE = "application/pdf"


@dataclass(frozen=True, slots=True)
class PollExports:
    """A row of the table "Par vote" in Documents: a poll pushed, with its number."""

    number: int
    poll: Poll

    @property
    def ready(self) -> bool:
        """Voting has stopped: the poll's exports answer 200 (an open poll's answer 409)."""
        return self.poll.status in exports.FINISHED


@console_context
def exports_context(
    request: Request, db: Session, access: ConsoleAccess, assembly: Assembly, keyring: Keyring
) -> dict[str, Any]:
    """Number of each pushed poll in the session order, as in the minutes and file names, and
    the rows of the table "Par vote" in that order, for the owner only (Documents): every poll
    pushed but a trial vote, which no export carries (D-I)."""
    numbers = exports.poll_numbers(db, assembly)
    rows: list[PollExports] = []
    if access.is_owner and numbers:
        pushed = db.scalars(
            select(Poll).where(Poll.assembly_id == assembly.id, Poll.id.in_(numbers))
        ).all()
        rows = sorted(
            (PollExports(numbers[poll.id], poll) for poll in pushed), key=lambda row: row.number
        )
    return {"exports_numbers": numbers, "exports_polls": rows}


def attachment(content: bytes, media_type: str, filename: str) -> Response:
    response = Response(content, media_type=media_type)
    ascii_name = filename.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    response.headers["Content-Disposition"] = (
        f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def demo_refused(request: Request) -> Response:
    """A demo account (FR-09a) exports nothing: the console shows these links greyed out."""
    email = request.app.state.settings.contact_email
    context = {
        "title": _("Exports need an account"),
        "message": _(
            "A demonstration exports nothing: no minutes, no CSV or JSON file. To get an "
            "account, write to %(email)s."
        )
        % {"email": email},
    }
    return render(request, "error.html", context, status_code=403)


def not_ready(request: Request, error: InvalidStateError) -> Response:
    context = {"title": _("Export not available yet"), "message": error_message(error)}
    return render(request, "error.html", context, status_code=409)


def log_export(
    database: Database, user: User, public_id: str, action: str, poll: Poll | None = None
) -> None:
    with database.write() as db:
        exports.record_export(db, user, public_id, action, poll=poll)


def file_name(pattern: str, public_id: str, number: int | None = None) -> str:
    return _(pattern) % {"id": public_id, "number": number}


@router.get("/manage/{public_id}/polls/{poll_id}/ballots.csv", dependencies=[SameSiteOnly])
def poll_ballots_csv(
    request: Request,
    public_id: OwnedSession,
    poll_id: RowId,
    user: ConsoleUser,
    database: DatabaseDep,
) -> Response:
    """Anonymous ballots in random order, for a spreadsheet (FR-60)."""
    if user.is_demo:
        return demo_refused(request)
    try:
        with database.read() as db:
            _session, record = exports.poll_record(db, user, public_id, poll_id)
            content = csv_export.ballots_csv(record)
    except InvalidStateError as error:
        return not_ready(request, error)
    log_export(database, user, public_id, "export.ballots_csv", record.poll)
    name = file_name(BALLOTS_CSV_NAME, public_id, record.number)
    return attachment(content, CSV_TYPE, name)


@router.get("/manage/{public_id}/polls/{poll_id}/export.json", dependencies=[SameSiteOnly])
def poll_json(
    request: Request,
    public_id: OwnedSession,
    poll_id: RowId,
    user: ConsoleUser,
    database: DatabaseDep,
) -> Response:
    """Definition, ballots, stored result and digest of one poll (FR-61)."""
    if user.is_demo:
        return demo_refused(request)
    try:
        with database.read() as db:
            session, record = exports.poll_record(db, user, public_id, poll_id)
            document = json_export.document(session, [record], now=utcnow())
    except InvalidStateError as error:
        return not_ready(request, error)
    log_export(database, user, public_id, "export.poll_json", record.poll)
    name = file_name(POLL_JSON_NAME, public_id, record.number)
    return attachment(json_export.dumps(document), JSON_TYPE, name)


@router.get("/manage/{public_id}/export.json", dependencies=[SameSiteOnly])
def session_json(
    request: Request, public_id: OwnedSession, user: ConsoleUser, database: DatabaseDep
) -> Response:
    """Every finished poll of the session, with its ballots and result (FR-61)."""
    if user.is_demo:
        return demo_refused(request)
    with database.read() as db:
        session = exports.session_record(db, user, public_id, with_ballots=True)
        document = json_export.document(session, session.finished, now=utcnow())
    log_export(database, user, public_id, "export.session_json")
    return attachment(
        json_export.dumps(document), JSON_TYPE, file_name(SESSION_JSON_NAME, public_id)
    )


@router.get("/manage/{public_id}/minutes.pdf", dependencies=[SameSiteOnly])
def minutes_pdf(
    request: Request, public_id: OwnedSession, user: ConsoleUser, database: DatabaseDep
) -> Response:
    """PDF minutes (FR-62), rendered once the read transaction has ended; 429 or 503 when the
    account is over its limit or the renderer is busy (``pdf_rendering``)."""
    if user.is_demo:
        return demo_refused(request)
    with pdf_rendering(request, user):
        with database.read() as db:
            session = exports.session_record(db, user, public_id)
        content = minutes.render(session, now=utcnow())
    log_export(database, user, public_id, "export.minutes_pdf")
    return attachment(content, PDF_TYPE, file_name(MINUTES_PDF_NAME, public_id))
