"""Session console page, live panel, console events, details and session lifecycle
(FR-13, FR-14, FR-84)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import partial
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy.orm import Session

from agaura.db import Database
from agaura.models import AccessMode, AssemblyStatus, BatchStatus
from agaura.security import Keyring
from agaura.services import access as access_service
from agaura.services import assemblies, console
from agaura.services.assemblies import AssemblyInput
from agaura.services.errors import NotFoundError, ServiceError
from agaura.web.deps import (
    CsrfProtected,
    DatabaseDep,
    HubDep,
    KeyringDep,
    LimiterDep,
    LoginRequiredError,
    SuperAdminRefusedError,
)
from agaura.web.limits import JOIN_ALERT, JOIN_FAILURES_PER_SESSION, join_failures
from agaura.web.manage.common import (
    CONSOLE_CONTEXT,
    ConsoleAccess,
    ConsoleSession,
    ConsoleUser,
    CurrentAccess,
    DelegateEndedError,
    OwnedSession,
    action_label,
    console_access,
    optional_date,
    optional_int,
    qr_svg,
    redirect,
    router,
    run_action,
    voter_url,
)
from agaura.web.manage.links import recap_context
from agaura.web.manage.timer import card_context as timer_context
from agaura.web.realtime import Holder, manage_channel, stream
from agaura.web.security import RateLimiter
from agaura.web.templating import flash, flash_error, render, templates


@dataclass(frozen=True)
class Step:
    """One step of the checklist shown before the opening (manage/_checklist.html).

    An optional step never counts in "%(done)s of %(total)s steps ready": the projection screen
    (nothing to check, ``done`` None) and the trial vote (``done`` once its draft exists, with
    ``poll`` its id). ``missing`` names what a step still lacks, in the order shown ("date",
    "place").
    """

    key: str  # details, tribune, agenda, polls, access, screen, trial
    done: bool | None  # None: nothing to check
    optional: bool = False
    missing: tuple[str, ...] = ()
    poll: int | None = None


def checklist(state: console.ConsoleState) -> list[Step]:
    assembly = state.assembly
    if assembly.access_mode is AccessMode.CODES:
        access_ready = any(batch.status is BatchStatus.ACTIVE for batch in state.batches)
    else:
        access_ready = True  # a session password always exists
    missing = tuple(
        name for name, value in (("date", assembly.date), ("place", assembly.place)) if not value
    )
    trial = next((poll.id for poll in state.drafts if poll.trial), None)
    return [
        Step("details", not missing, missing=missing),
        Step("tribune", bool(assembly.tribune)),
        Step("agenda", bool(assembly.agenda)),
        Step("polls", any(not poll.trial for poll in state.drafts)),
        Step("access", access_ready),
        Step("screen", None, optional=True),
        Step("trial", trial is not None, optional=True, poll=trial),
    ]


def join_alert(limiter: RateLimiter, public_id: str) -> dict[str, Any]:
    """Refused passwords or codes on the session, once there are enough to warn the tribune in
    the status bar (``agaura.web.limits``)."""
    failures = join_failures(limiter, public_id)
    return {
        "join_alert": failures if failures >= JOIN_ALERT else 0,
        "join_lockdown": failures >= JOIN_FAILURES_PER_SESSION[0],
    }


def render_console(
    request: Request,
    access: ConsoleAccess,
    database: Database,
    keyring: Keyring,
    limiter: RateLimiter,
    *,
    status_code: int = 200,
) -> Response:
    """The console page, for the owner or, limited to its rights, for a delegate (FR-92).

    A delegate never gets the session password, the QR code that carries it, or the refused
    joins warning: the cards that use them are the owner's. The page carries secrets (session
    password, keys of the delegate links), so browsers must not store it.
    """
    user = access.user
    with database.read() as db:
        state = console.console_state(db, user, access.public_id)
        assembly = state.assembly
        url = voter_url(request, assembly.public_id)
        context: dict[str, Any] = {
            "state": state,
            "assembly": assembly,
            "voter_url": url,
            "action_label": action_label,
            "checklist": checklist(state),
            "can": access.can,
            "password": None,
            "join_qr": None,
            "join_alert": 0,
            "join_lockdown": False,
        }
        if access.is_owner:
            password = access_service.session_password(db, keyring, user, assembly.public_id)
            context["password"] = password
            if assembly.access_mode is AccessMode.PASSWORD:
                context["join_qr"] = qr_svg(f"{url}#k={quote(password, safe='')}")
            context.update(join_alert(limiter, assembly.public_id))
        for provider in CONSOLE_CONTEXT:
            context.update(provider(request, db, access, assembly, keyring))
        response = render(request, "manage/console.html", context, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/manage/{public_id}", response_class=HTMLResponse)
def session_console(
    request: Request,
    public_id: ConsoleSession,
    access: CurrentAccess,
    database: DatabaseDep,
    keyring: KeyringDep,
    limiter: LimiterDep,
) -> Response:
    if request.path_params["public_id"] != public_id:
        return RedirectResponse(f"/manage/{public_id}", status_code=303)
    return render_console(request, access, database, keyring, limiter)


@router.get("/manage/{public_id}/panel", response_class=HTMLResponse)
def live_panel(
    request: Request,
    public_id: ConsoleSession,
    access: CurrentAccess,
    database: DatabaseDep,
    keyring: KeyringDep,
    limiter: LimiterDep,
) -> Response:
    """The live parts of the console (``manage/_panel.html``), fetched by manage.js on every
    console event: what another console may change, limited to the rights of the person at
    this one. Rendered without ``render()``, which would consume the flash notice meant for
    the console page."""
    can = access.can
    with database.read() as db:
        panel = console.live_panel(db, access.user, public_id)
        in_session = panel.assembly.status is AssemblyStatus.IN_SESSION
        # The "Next poll" command, polls right only: the drafts in their order.
        drafts = console.drafts(db, access.user, public_id) if in_session and can.polls else []
        context = {
            "csrf_token": request.state.csrf_token,
            "panel": panel,
            "assembly": panel.assembly,
            "can": can,
            "in_session": in_session,
            "drafts": drafts,
            **timer_context(panel.assembly),
            **recap_context(db, keyring, access, panel.assembly),
            **(join_alert(limiter, public_id) if access.is_owner else {"join_alert": 0}),
        }
        response = templates.TemplateResponse(request, "manage/_panel.html", context)
    response.headers["Cache-Control"] = "no-store"
    return response


def _delegate_id(access: ConsoleAccess) -> int | None:
    return access.delegate.id if access.delegate is not None else None


def still_at_console(request: Request, opened: ConsoleAccess) -> bool:
    """Whether whoever opened a console stream may still follow the session: the same owner,
    still logged in, or the same delegate link, still in use. ``console_access`` again, on a
    copy of the request without its cached answers."""
    fresh = Request({**request.scope, "state": {}})
    try:
        current = console_access(fresh, opened.public_id)
    except (LoginRequiredError, SuperAdminRefusedError, DelegateEndedError, NotFoundError):
        return False
    return _delegate_id(current) == _delegate_id(opened)


async def _events_slot(
    hub: HubDep, response: Response, public_id: ConsoleSession, access: CurrentAccess
) -> AsyncIterator[bool]:
    """One of the few streams the owner, or one delegate link, may keep open on a session
    (``realtime.STREAM_CAPS``), else 429: manage.js then polls."""
    holder = Holder("console", (public_id, _delegate_id(access)))
    with hub.hold(manage_channel(public_id), holder) as held:
        if not held:
            response.status_code = 429
        yield held


@router.get("/manage/{public_id}/events", response_class=EventSourceResponse)
async def console_events(
    request: Request,
    public_id: ConsoleSession,
    access: CurrentAccess,
    hub: HubDep,
    held: Annotated[bool, Depends(_events_slot)],
) -> AsyncIterator[ServerSentEvent]:
    """Ends once its holder loses the console: logged out, delegate link revoked, session
    closed (for delegates) or deleted."""
    if not held:
        return
    allowed = partial(still_at_console, request, access)
    async for message in stream(hub, manage_channel(public_id), allowed):
        yield ServerSentEvent(event=message.event, data=message.data)


@router.post("/manage/{public_id}/details", dependencies=[CsrfProtected])
def edit_details(
    request: Request,
    public_id: OwnedSession,
    user: ConsoleUser,
    title: Annotated[str, Form()] = "",
    date: Annotated[str, Form()] = "",
    place: Annotated[str, Form()] = "",
    registered_count: Annotated[str, Form()] = "",
    quorum_rule: Annotated[str, Form()] = "",
) -> Response:
    def action(db: Session) -> None:
        data = AssemblyInput(
            title=title,
            date=optional_date(date),
            place=place,
            registered_count=optional_int(registered_count, minimum=1),
            quorum_rule=quorum_rule,
        )
        assemblies.update_assembly(db, user, public_id, data)

    return run_action(request, public_id, action, notice="details_saved")


@router.post("/manage/{public_id}/open", dependencies=[CsrfProtected])
def open_session(request: Request, public_id: OwnedSession, user: ConsoleUser) -> Response:
    return run_action(
        request,
        public_id,
        lambda db: assemblies.open_assembly(db, user, public_id),
        notice="session_opened",
        voters="session",
    )


@router.post("/manage/{public_id}/close", dependencies=[CsrfProtected])
def close_session(request: Request, public_id: OwnedSession, user: ConsoleUser) -> Response:
    return run_action(
        request,
        public_id,
        lambda db: assemblies.close_assembly(db, user, public_id),
        notice="session_closed",
        voters="session",
    )


@router.post("/manage/{public_id}/delete", dependencies=[CsrfProtected])
def delete_session(
    request: Request,
    public_id: OwnedSession,
    user: ConsoleUser,
    database: DatabaseDep,
    confirm: Annotated[str, Form()] = "",
) -> Response:
    if confirm != "yes":
        response = redirect(public_id, "#danger")
        flash_error(response, request, "required")
        return response
    try:
        with database.write() as db:
            assemblies.delete_assembly(db, user, public_id)
    except ServiceError as error:
        if isinstance(error, NotFoundError):
            raise
        response = redirect(public_id, "#danger")
        flash_error(response, request, error.code)
        return response
    # Screens still open on this session refetch at once and turn into the 404 page.
    request.app.state.hub.publish(manage_channel(public_id), "turnout", "console")
    response = RedirectResponse("/dashboard", status_code=303)
    flash(response, request, "session_deleted")
    return response
