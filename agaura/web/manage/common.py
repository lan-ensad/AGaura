"""Shared pieces of the console area: the router, dependencies, helpers and message codes.

Every module of ``agaura.web.manage`` declares its routes on the single ``router`` below, so the
console stays one flat list of routes (the generated isolation tests walk it). Modules that need
extra data on the console page register a context provider with ``@console_context``.

Who may use a route: the owner of the session, logged in, may use them all. Someone holding a
delegate link of the session (FR-92) may use the routes whose session dependency names one of
the link's rights: ``AgendaSession``, ``NotesSession``, ``PollsSession``, ``TimerSession``,
``ScreenSession``, ``HeadcountSession``,
or ``ConsoleSession`` for the reads every delegate needs (console page, live panel, events).
``OwnedSession`` is the owner's only: a delegate gets 404 there, as for a session of another
account. In every case ``ConsoleUser`` is the owner of the session, so services keep filtering
on it (FR-07).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Any

import segno
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from markupsafe import Markup
from sqlalchemy.orm import Session

from agaura.i18n import N_, _
from agaura.models import Assembly, Delegate, Poll, User
from agaura.pdf import render_slot
from agaura.security import Keyring
from agaura.services import assemblies, audit, delegates, polls
from agaura.services.delegates import Right
from agaura.services.errors import InvalidInputError, NotFoundError, ServiceError
from agaura.web.deps import (
    SuperAdminRefusedError,
    logged_in_user,
    optional_user,
    public_base_url,
)
from agaura.web.messages import ERRORS, NOTICES
from agaura.web.realtime import live_channel, manage_channel
from agaura.web.security import DELEGATE_COOKIE, RateLimiter, read_cookie
from agaura.web.templating import flash, flash_error

router = APIRouter()

ERRORS.update(
    {
        "email_not_verified": N_("Confirm your email address before creating a session."),
        "invalid_number": N_("Enter a whole number."),
        "invalid_date": N_("Enter a valid date."),
        "invalid_seats": N_("The number of seats must be between 1 and the number of candidates."),
    }
)
NOTICES.update(
    {
        "session_created": N_("Session created. Prepare the polls, then open the session."),
        "session_opened": N_("The session is open: voters can join."),
        "session_closed": N_("The session is closed. Every voter access has ended."),
        "session_deleted": N_("Session deleted."),
        "details_saved": N_("Details saved."),
        "password_changed": N_("Session password changed."),
        "mode_changed": N_("Access mode changed."),
        "entries_locked": N_("Entries locked: nobody new can join."),
        "entries_unlocked": N_("Entries unlocked."),
        "headcount_saved": N_(
            "Headcount saved and added to the attendance record; the console warns when "
            "accesses or ballots exceed it."
        ),
        "codes_revoked": N_("Codes revoked: they no longer work."),
        "poll_created": N_("Poll created as a draft."),
        "poll_opened": N_("Poll pushed to voters."),
        "poll_closed": N_("Poll closed; result computed."),
        "poll_cancelled": N_("Poll cancelled."),
        "poll_published": N_("Result published."),
        "poll_dismissed": N_("Result hidden."),
        "poll_deleted": N_("Draft deleted."),
        "poll_duplicated": N_("Copy created as a draft."),
    }
)

# Session log (FR-84), action code → label. Modules add their own with ``ACTIONS.update``.
ACTIONS = {
    "assembly.created": N_("Session created"),
    "assembly.updated": N_("Details edited"),
    "assembly.opened": N_("Session opened"),
    "assembly.closed": N_("Session closed"),
    "access.password_regenerated": N_("New session password generated"),
    "access.password_changed": N_("Session password replaced"),
    "access.mode_changed": N_("Access mode changed"),
    "access.entries_locked": N_("Entries locked"),
    "access.entries_unlocked": N_("Entries unlocked"),
    "access.headcount_set": N_("Headcount entered"),
    "access.screen_link_regenerated": N_("Screen link regenerated"),
    "access.public_link_enabled": N_("Public results link enabled"),
    "access.public_link_disabled": N_("Public results link disabled"),
    "codes.batch_created": N_("Individual codes generated"),
    "codes.batch_revoked": N_("Individual codes revoked"),
    "poll.created": N_("Poll created"),
    "poll.updated": N_("Poll edited"),
    "poll.deleted": N_("Draft deleted"),
    "poll.duplicated": N_("Poll duplicated"),
    "polls.copied": N_("Polls copied from another session"),
    "polls.reordered": N_("Drafts reordered"),
    "poll.opened": N_("Poll pushed"),
    "poll.closed": N_("Poll closed"),
    "poll.cancelled": N_("Poll cancelled"),
    "poll.published": N_("Result published"),
    "poll.dismissed": N_("Result hidden"),
}


def action_label(action: str) -> str:
    return _(ACTIONS[action]) if action in ACTIONS else action


# --- Who is at the console ---------------------------------------------------------------------

# ``DELEGATE_COOKIE`` (path /manage/{ID}) holds the token of a delegate link.
DELEGATE_COOKIE_AGE = 30 * 24 * 3600  # the link itself ends with the session
ANY_RIGHT = "any"  # routes every delegate of the session may use (reads of the console page)


@dataclass(frozen=True, slots=True)
class Can:
    """What the console shows, for templates: ``can.polls``, ``can.owner``…"""

    owner: bool
    agenda: bool
    notes: bool
    polls: bool
    timer: bool
    screen: bool
    headcount: bool

    @property
    def key(self) -> str:
        """What the page shows, in one word (``data-access``): the console reloads itself when
        the owner changes the rights of the link it was opened with."""
        if self.owner:
            return "owner"
        names = ("agenda", "notes", "polls", "timer", "screen", "headcount")
        return ",".join(name for name in names if getattr(self, name))

    @property
    def lone_tool(self) -> bool:
        """A delegate link with one tool (session notes, chronometer or headcount) and no step
        of the session (agenda, polls): its console comes down to that card, whose button is
        then the primary one. Elsewhere in the cockpit those buttons are secondary: tools, not
        the next step of the session (R12)."""
        tools = (self.notes, self.timer, self.headcount)
        return not (self.owner or self.agenda or self.polls) and sum(tools) == 1


@dataclass(frozen=True, slots=True)
class ConsoleAccess:
    """The person at the console of a session: its owner, logged in, or a delegate link.

    ``user`` is the owner of the session in both cases: services act for it. ``delegate`` is
    None for the owner.
    """

    public_id: str  # canonical
    user: User
    delegate: Delegate | None = None

    @property
    def is_owner(self) -> bool:
        return self.delegate is None

    def allows(self, right: Right | str | None) -> bool:
        """``right`` None: the owner only; ``ANY_RIGHT``: any delegate too."""
        if self.delegate is None:
            return True
        return right == ANY_RIGHT or (right is not None and right in self.delegate.rights)

    @property
    def can(self) -> Can:
        return Can(
            owner=self.is_owner,
            agenda=self.allows(Right.AGENDA),
            notes=self.allows(Right.NOTES),
            polls=self.allows(Right.POLLS),
            timer=self.allows(Right.TIMER),
            screen=self.allows(Right.SCREEN),
            headcount=self.allows(Right.HEADCOUNT),
        )


class DelegateEndedError(Exception):
    """A delegate cookie that no longer opens the console (link revoked, session closed);
    answered with a page that says so, and the cookie is removed."""

    def __init__(self, public_id: str) -> None:
        super().__init__(public_id)
        self.public_id = public_id


def console_access(request: Request, public_id: str) -> ConsoleAccess:
    """Dependency: who may act on this session, resolved once per request.

    The owner first: a browser logged in to the account that owns the session is the owner,
    whatever delegate cookie it holds. Else a delegate link of this session, from its cookie.
    Else the request is refused: a super-admin gets its 403 page before any session is looked
    up, a stale delegate cookie the page of an ended link, an anonymous visitor the login page,
    and another account the 404 page.
    """
    found = getattr(request.state, "console_access", None)
    if found is not None and found.public_id == assemblies.normalize_public_id(public_id):
        return found
    user = optional_user(request)
    if user is not None and user.is_superadmin:
        raise SuperAdminRefusedError
    token = read_cookie(request, DELEGATE_COOKIE)
    access = None
    database = request.app.state.database
    with database.read() as db:
        if user is not None:
            try:
                owned = assemblies.get_assembly(db, user, public_id)
                access = ConsoleAccess(owned.public_id, user)
            except NotFoundError:
                pass
        if access is None and token:
            delegate = delegates.authenticate(db, public_id, token)
            if delegate is not None:
                assembly = delegate.assembly
                access = ConsoleAccess(assembly.public_id, assembly.owner, delegate)
    if access is None:
        if token:
            raise DelegateEndedError(public_id)
        logged_in_user(request)  # anonymous: to the login page
        raise NotFoundError("assembly")
    if access.delegate is not None and delegates.needs_touch(access.delegate):
        with database.write() as db:
            delegates.touch(db, access.delegate.id)
    request.state.console_access = access
    return access


def session_for(right: Right | str | None) -> Callable[..., str]:
    """Dependency factory: the canonical session ID, when the person at the console may use a
    route that needs ``right`` (None: the owner only); else 404, as for a missing session."""

    def dependency(access: Annotated[ConsoleAccess, Depends(console_access)]) -> str:
        if not access.allows(right):
            raise NotFoundError("assembly")
        return access.public_id

    return dependency


def console_user(access: Annotated[ConsoleAccess, Depends(console_access)]) -> User:
    """Dependency: the owner of the session, for whom services act."""
    return access.user


OwnedSession = Annotated[str, Depends(session_for(None))]
ConsoleSession = Annotated[str, Depends(session_for(ANY_RIGHT))]
AgendaSession = Annotated[str, Depends(session_for(Right.AGENDA))]
NotesSession = Annotated[str, Depends(session_for(Right.NOTES))]
PollsSession = Annotated[str, Depends(session_for(Right.POLLS))]
TimerSession = Annotated[str, Depends(session_for(Right.TIMER))]
ScreenSession = Annotated[str, Depends(session_for(Right.SCREEN))]
HeadcountSession = Annotated[str, Depends(session_for(Right.HEADCOUNT))]
ConsoleUser = Annotated[User, Depends(console_user)]
CurrentAccess = Annotated[ConsoleAccess, Depends(console_access)]


@contextmanager
def console_write(request: Request) -> Iterator[Session]:
    """The write transaction of a console action. Through a delegate link, the audit entries
    it records name the delegate (``audit.act_as_delegate``)."""
    with request.app.state.database.write() as db:
        access = getattr(request.state, "console_access", None)
        if access is not None and access.delegate is not None:
            audit.act_as_delegate(db, access.delegate.id)
        yield db


# --- Console page context ---------------------------------------------------------------------

ConsoleContext = Callable[[Request, Session, ConsoleAccess, Assembly, Keyring], dict[str, Any]]
CONSOLE_CONTEXT: list[ConsoleContext] = []


def console_context(provider: ConsoleContext) -> ConsoleContext:
    """Register extra template variables for ``manage/console.html``.

    The provider runs inside the console's read transaction, after the access check, for the
    owner and for delegates alike: it must give a delegate only what its rights cover
    (``access.allows``), with the same keys. Its keys must not clash with another provider's
    (prefix them with the module's name).
    """
    CONSOLE_CONTEXT.append(provider)
    return provider


# --- PDF documents ----------------------------------------------------------------------------

# Minutes and code cards per account: generous for a tribune who downloads the minutes again
# after each correction, while one account keeps the renderer busy a third of the time at most
# (10 batches of 2,000 cards, about 20 seconds each).
PDF_RENDERS_PER_ACCOUNT = (10, 600.0)


class TooManyRendersError(Exception):
    """Past ``PDF_RENDERS_PER_ACCOUNT``; answered with a 429 page (``agaura.main``)."""


def pdf_renders_key(user: User) -> str:
    return f"pdf-renders:{user.id}"


@contextmanager
def pdf_rendering(request: Request, user: User) -> Iterator[None]:
    """Reserve the renderer for one PDF document of ``user`` (``agaura.pdf.render_slot``).

    Every attempt counts, including the ones refused because another document holds the
    renderer: retries then cannot pile up worker threads waiting for it.
    """
    limiter: RateLimiter = request.app.state.limiter
    key = pdf_renders_key(user)
    if limiter.exceeded(key, *PDF_RENDERS_PER_ACCOUNT):
        raise TooManyRendersError
    limiter.record(key)
    with render_slot():
        yield


# --- Helpers ----------------------------------------------------------------------------------


def qr_svg(url: str) -> Markup:
    """QR code as inline SVG (strict CSP: no image request, no script)."""
    code = segno.make(url, error="m")
    return Markup(code.svg_inline(scale=1, border=2, omitsize=True, dark="#000", light="#fff"))  # noqa: S704 (generated SVG)


def voter_url(request: Request, public_id: str) -> str:
    return f"{public_base_url(request)}/{public_id}"


# Digits of a number typed in a form: more than any count or duration needs, and far below what
# an SQLite integer holds (19 digits) or Python's int() accepts (4,300).
MAX_NUMBER_DIGITS = 9


def optional_int(raw: str, *, minimum: int = 0) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    # ``isdigit`` accepts superscripts and other scripts' digits that ``int`` refuses.
    if (
        not (raw.isascii() and raw.isdigit())
        or len(raw.lstrip("0")) > MAX_NUMBER_DIGITS
        or int(raw) < minimum
    ):
        raise InvalidInputError("invalid_number")
    return int(raw)


def optional_date(raw: str) -> dt.date | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError as error:
        raise InvalidInputError("invalid_date") from error


def redirect(public_id: str, anchor: str = "") -> RedirectResponse:
    return RedirectResponse(f"/manage/{public_id}{anchor}", status_code=303)


def from_script(request: Request) -> bool:
    """A form sent by a console script with ``fetch`` rather than by the browser."""
    return request.headers.get("x-requested-with") == "fetch"


def run_action(
    request: Request,
    public_id: str,
    action: Callable[[Session], object],
    *,
    notice: str,
    voters: str | None = None,
    anchor: str = "",
    in_place: bool = False,
) -> Response:
    """Run ``action`` in a write transaction, notify pages, redirect back to the console.

    ``voters`` (``polls``, ``results`` or ``session``) also tells voter pages to refresh. The
    console channel always receives ``turnout``/``console``, which the projection screen follows
    too.

    ``in_place``: the form belongs to a live part, which manage.js sends with ``fetch``
    (``form[data-live-form]``). Such a request gets 204 without a notice, the console then
    fetching its live parts; a refused one gets 400 and keeps its message in the flash cookie,
    for the console page that manage.js loads again. Without script, the usual redirect.
    """
    script = in_place and from_script(request)
    try:
        with console_write(request) as db:
            action(db)
    except NotFoundError:
        raise
    except ServiceError as error:
        response = Response(status_code=400) if script else redirect(public_id, anchor)
        flash_error(response, request, error.code)
        return response
    hub = request.app.state.hub
    if voters:
        hub.publish(live_channel(public_id), "update", voters)
    hub.publish(manage_channel(public_id), "turnout", "console")
    if script:
        return Response(status_code=204)
    response = redirect(public_id, anchor)
    flash(response, request, notice)
    return response


def poll_action(
    request: Request,
    public_id: str,
    user: User,
    poll_id: int,
    action: Callable[[Session, Poll], object],
    *,
    notice: str,
    voters: str | None = None,
    anchor: str = "#drafts",
) -> Response:
    """``run_action`` on a poll that must belong to the session in the URL; by default back to
    the drafts, where a duplicate lands and a draft is deleted."""

    def checked(db: Session) -> None:
        action(db, owned_poll(db, user, public_id, poll_id))

    return run_action(request, public_id, checked, notice=notice, voters=voters, anchor=anchor)


def owned_poll(db: Session, user: User, public_id: str, poll_id: int) -> Poll:
    """A poll of the current account that belongs to the session in the URL, else 404."""
    poll = polls.get_poll(db, user, poll_id)
    if poll.assembly.public_id != public_id:
        raise NotFoundError("poll")
    return poll
