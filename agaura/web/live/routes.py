"""Voter pages (SPEC §4.3 to §4.5, FR-21 to FR-25, FR-40 to FR-46).

``/{session_id}`` shows the join form, then the live page once the device holds a valid
credential (cookie ``agaura_voter``, scoped to the session path). The live page shows only the
open polls and the results published to voters; it refreshes itself through Server-Sent
Events, with a polling fallback (``static/js/live.js``).

Every ballot step is a plain HTML form (NFR-05): ballot → summary → submission, then
POST/redirect/GET back to the live page. With JavaScript, the same routes answer with the HTML
of the poll's section only (request header ``X-Requested-With: fetch``).

Secrecy: nothing here logs, stores or echoes ballot content, except the summary shown to the
voter who is filling in the ballot.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.datastructures import FormData

from agaura.db import Database
from agaura.i18n import N_
from agaura.models import AccessMode, Assembly, AssemblyStatus, Poll, utcnow
from agaura.security import SESSION_PASSWORD_DIGITS, Keyring
from agaura.services import access, assemblies, ballots, live
from agaura.services.errors import (
    AccessDeniedError,
    AlreadyVotedError,
    InvalidInputError,
    InvalidStateError,
    NotFoundError,
)
from agaura.web.deps import (
    CsrfProtected,
    DatabaseDep,
    HubDep,
    KeyringDep,
    LimiterDep,
    RowId,
)
from agaura.web.limits import (
    address_refused,
    charge_join_attempt,
    join_alert_due,
    join_keys,
    record_unknown_id,
    refund_join_attempt,
    scanning,
)
from agaura.web.live.ballot_forms import (
    BLANK_WITH_CHOICE,
    Choice,
    check_choice,
    parse_choice,
    refuse_blank_with_choice,
)
from agaura.web.live.padding import padded_section
from agaura.web.messages import ERRORS, NOTICES, error_message
from agaura.web.realtime import Holder, live_channel, manage_channel, stream
from agaura.web.security import (
    VOTER_COOKIE,
    RateLimiter,
    delete_cookie,
    read_cookie,
    set_cookie,
)
from agaura.web.templating import flash, render

router = APIRouter()

VOTER_COOKIE_MAX_AGE = 24 * 3600
FRAGMENT_HEADER = "x-requested-with"
FRAGMENT_VALUE = "fetch"

VOTE_RECORDED = N_("Your vote has been recorded.")
ERRORS.update(
    {
        "live_no_grade": N_("Grade at least one option, or vote blank."),
        "live_no_rank": N_("Rank at least one option, or vote blank."),
        BLANK_WITH_CHOICE: N_(
            "A blank vote cannot be combined with other choices. Your choices are kept below; "
            "to vote blank, choose the blank vote alone."
        ),
        "live_join_failed": N_(
            "Unable to join. Check the session address and the password or code. If they are "
            "right, the session does not accept new entries for now: try again in a few minutes."
        ),
    }
)
NOTICES.update(
    {
        "live_vote_recorded": VOTE_RECORDED,
        "live_already_voted": N_("Your vote has already been recorded."),
    }
)


def _public_id(session_id: str) -> str:
    public_id = assemblies.normalize_public_id(session_id)
    if public_id is None:
        raise NotFoundError("assembly")
    return public_id


def _voter_token(request: Request) -> str | None:
    return read_cookie(request, VOTER_COOKIE)


def _wants_fragment(request: Request) -> bool:
    return request.headers.get(FRAGMENT_HEADER) == FRAGMENT_VALUE


def _private(response: Response) -> Response:
    """Pages built for one voter: never kept by a cache."""
    response.headers["Cache-Control"] = "no-store"
    return response


def _home(public_id: str) -> RedirectResponse:
    return RedirectResponse(f"/{public_id}", status_code=303)


def _forget_device(response: Response, request: Request, public_id: str) -> None:
    delete_cookie(response, request, VOTER_COOKIE, path=f"/{public_id}")


async def _form_data(request: Request) -> FormData:
    return await request.form()


FormDep = Annotated[FormData, Depends(_form_data)]


# --- Join -----------------------------------------------------------------------------------


def _password_field(keyring: Keyring, assembly: Assembly) -> dict[str, Any]:
    """Keyboard and hint of the session password field (D10), from the stored password.

    Disclosure: the join page tells only that the current password is made of digits, and that
    it has 8 of them when it has the shape of a generated one: the documented default (D10).
    A password the tribune chose with letters or other signs gets the usual keyboard and hint.
    Spaces never count, since the join ignores them (FR-24).
    """
    if assembly.access_mode is not AccessMode.PASSWORD:
        return {}
    password = "".join(keyring.decrypt(assembly.access_password_enc).split())
    numeric = password.isascii() and password.isdigit()
    generated = numeric and len(password) == SESSION_PASSWORD_DIGITS
    return {
        "password_numeric": numeric,
        "password_digits": SESSION_PASSWORD_DIGITS if generated else None,
    }


def _entry_page(
    request: Request,
    keyring: Keyring,
    assembly: Assembly,
    *,
    error: str | None = None,
    status_code: int = 200,
    results: list[Any] | None = None,
) -> Response:
    """Join form while in session; otherwise a page explaining the session's state, with the
    results published to voters for a device that took part (``results``)."""
    context: dict[str, Any] = {
        "assembly": assembly,
        "public_id": assembly.public_id,
        "error": error,
        "results": results or [],
    }
    if assembly.status is AssemblyStatus.IN_SESSION:
        context.update(_password_field(keyring, assembly))
        return _private(render(request, "live/join.html", context, status_code=status_code))
    return _private(render(request, "live/status.html", context, status_code=status_code))


def _neutral_entry_page(
    request: Request, public_id: str, *, error: str | None = None, status_code: int = 200
) -> Response:
    """Join form for a scanning address: the same for every ID, whether the session exists or
    not and whatever its state (no title, no state, one label for passwords and codes)."""
    context: dict[str, Any] = {"assembly": None, "public_id": public_id, "error": error}
    return _private(render(request, "live/join.html", context, status_code=status_code))


def _neutral_join_failed(request: Request, public_id: str) -> Response:
    """One answer for an unknown ID, a session not in session, a wrong or refused secret."""
    error = error_message("live_join_failed")
    return _neutral_entry_page(request, public_id, error=error, status_code=400)


def _find_session(
    request: Request, db: Any, limiter: RateLimiter, public_id: str
) -> Assembly | None:
    """The session behind a well-formed ID; an unknown ID counts towards the scanning limit
    (``agaura.web.limits``). A session of a disabled account looks unknown, without counting:
    its voters reloading their page are no scan."""
    assembly = assemblies.find_assembly(db, public_id)
    if assembly is None:
        record_unknown_id(limiter, request)
        return None
    return assembly if assembly.owner.disabled_at is None else None


@router.get("/{session_id}", response_class=HTMLResponse)
def session_page(
    request: Request,
    session_id: str,
    database: DatabaseDep,
    keyring: KeyringDep,
    limiter: LimiterDep,
) -> Response:
    public_id = _public_id(session_id)
    token = _voter_token(request)
    with database.read() as db:
        credential = access.credential_for(db, public_id, token)
        if credential is None and scanning(limiter, request):
            # Neutral page: other spellings redirect before any lookup, like for every ID.
            if session_id != public_id:
                return _home(public_id)
            _find_session(request, db, limiter, public_id)  # counted, never shown
            assembly = None
        else:
            assembly = _find_session(request, db, limiter, public_id)
            if assembly is None:
                raise NotFoundError("assembly")
            if session_id != public_id:
                return _home(public_id)
        view = None if credential is None else live.live_view(db, credential)
        # A device that took part keeps the results published to voters once the session is
        # closed (its credential votes no more); any other visitor gets the closing message.
        former = (
            credential is None
            and assembly is not None
            and access.former_credential(db, public_id, token) is not None
        )
        results = live.results_for_voters(db, assembly.id) if former and assembly else []

    if assembly is None:
        response = _neutral_entry_page(request, public_id)
    elif view is not None:
        context = {"assembly": assembly, "public_id": public_id, "view": view}
        return _private(render(request, "live/page.html", context))
    else:
        stale = bool(token) and assembly.status is AssemblyStatus.IN_SESSION
        response = _entry_page(
            request,
            keyring,
            assembly,
            error=error_message("invalid_credential") if stale else None,
            results=results,
        )
    if token and not former:
        _forget_device(response, request, public_id)
    return response


@router.post("/{session_id}/join", dependencies=[CsrfProtected])
def join(
    request: Request,
    session_id: str,
    database: DatabaseDep,
    keyring: KeyringDep,
    hub: HubDep,
    limiter: LimiterDep,
    secret: Annotated[str, Form()] = "",
) -> Response:
    """Join with the session password or an individual code.

    A scanning address (``agaura.web.limits``) still joins with the right secret, under the same
    join limits; any other outcome gets the neutral page with one error, the same whether the
    session exists or not, so the answer never tells which. A session in password mode that
    reached its access cap (FR-27) refuses new devices with 409, never counted as a failure.
    """
    public_id = _public_id(session_id)
    with database.read() as db:
        if access.credential_for(db, public_id, _voter_token(request)) is not None:
            # A second scan of the QR code must not create a second credential.
            return _home(public_id)
        neutral = scanning(limiter, request)
        assembly = _find_session(request, db, limiter, public_id)

    keys = join_keys(request, public_id)
    if neutral:
        if address_refused(limiter, keys):  # this address, whatever the ID
            return _neutral_entry_page(
                request, public_id, error=error_message("too_many_attempts"), status_code=429
            )
        in_session = assembly is not None and assembly.status is AssemblyStatus.IN_SESSION
        if not in_session or not charge_join_attempt(limiter, keys):
            return _neutral_join_failed(request, public_id)
    elif assembly is None:
        raise NotFoundError("assembly")
    elif assembly.status is not AssemblyStatus.IN_SESSION:
        # Refused before any check of the secret: nothing to guess before the opening.
        return _entry_page(request, keyring, assembly, status_code=409)
    elif not charge_join_attempt(limiter, keys):
        return _entry_page(
            request, keyring, assembly, error=error_message("too_many_attempts"), status_code=429
        )

    # From here the attempt counts as a failure, so that concurrent wrong secrets see each
    # other; it is taken back unless the secret turns out wrong.
    try:
        with database.write() as db:
            token = access.join(db, keyring, public_id, secret).token
    except AccessDeniedError as error:
        if error.code == "session_not_in_session":  # changed meanwhile
            refund_join_attempt(limiter, keys)
            return _neutral_join_failed(request, public_id) if neutral else _home(public_id)
        if error.code in (access.ACCESS_CAP_REACHED, access.DEMO_VOTERS_FULL):
            # The right password: not a failure, so it never feeds the join limits. The join
            # that reached the cap already refreshed the console, which shows the warning.
            refund_join_attempt(limiter, keys)
            if neutral:
                return _neutral_join_failed(request, public_id)
            return _entry_page(
                request, keyring, assembly, error=error_message(error), status_code=409
            )
        if join_alert_due(limiter, keys):
            hub.publish(manage_channel(public_id), "turnout", "console")  # warn the tribune
        if neutral:
            return _neutral_join_failed(request, public_id)
        return _entry_page(request, keyring, assembly, error=error_message(error), status_code=400)
    except BaseException:
        refund_join_attempt(limiter, keys)  # database error: not a guess
        raise
    refund_join_attempt(limiter, keys)

    response = _home(public_id)
    set_cookie(
        response,
        request,
        VOTER_COOKIE,
        token,
        max_age=VOTER_COOKIE_MAX_AGE,
        path=f"/{public_id}",
    )
    hub.publish(manage_channel(public_id), "turnout")
    return response


@router.post("/{session_id}/leave", dependencies=[CsrfProtected])
def leave(request: Request, session_id: str, database: DatabaseDep) -> Response:
    """End the session on this device ("Terminer", FR-46)."""
    public_id = _public_id(session_id)
    with database.write() as db:
        access.leave(db, public_id, _voter_token(request))
    response = _home(public_id)
    _forget_device(response, request, public_id)
    flash(response, request, "left_session")
    return response


# --- Live content -----------------------------------------------------------------------------


@router.get("/{session_id}/live", response_class=HTMLResponse)
def live_content(request: Request, session_id: str, database: DatabaseDep) -> Response:
    """The live page's sections, for the script's refreshes. 401: the page must reload."""
    public_id = _public_id(session_id)
    with database.read() as db:
        credential = access.credential_for(db, public_id, _voter_token(request))
        view = None if credential is None else live.live_view(db, credential)
    if view is None:
        return _private(Response(status_code=401))
    context = {"public_id": public_id, "view": view}
    return _private(render(request, "live/_content.html", context))


@dataclass(frozen=True, slots=True)
class _Follower:
    """A voter's device following the live channel (kept in memory for the stream only)."""

    public_id: str
    token: str
    credential_id: int


def _events_access(
    request: Request, session_id: str, database: DatabaseDep, response: Response
) -> _Follower | None:
    """The voter with a valid credential; otherwise answer 204.

    ``EventSource`` stops reconnecting on 204; the script then refreshes the page content,
    which answers 401 and makes the page reload into the join form.
    """
    public_id = assemblies.normalize_public_id(session_id)
    token = _voter_token(request)
    if public_id is not None and token:
        with database.read() as db:
            credential = access.credential_for(db, public_id, token)
            if credential is not None:
                return _Follower(public_id, token, credential.id)
    response.status_code = 204
    return None


async def _events_slot(
    hub: HubDep,
    response: Response,
    follower: Annotated[_Follower | None, Depends(_events_access)],
) -> AsyncIterator[_Follower | None]:
    """One of the few streams a device may keep open (``realtime.STREAM_CAPS``), else 429:
    live.js then polls and tries again later."""
    if follower is None:
        yield None
        return
    holder = Holder("voter", (follower.credential_id,))
    with hub.hold(live_channel(follower.public_id), holder) as held:
        if not held:
            response.status_code = 429
        yield follower if held else None


@router.get("/{session_id}/events", response_class=EventSourceResponse)
async def events(
    database: DatabaseDep,
    hub: HubDep,
    follower: Annotated[_Follower | None, Depends(_events_slot)],
) -> AsyncIterator[ServerSentEvent]:
    """Ends once the credential no longer opens the session (revoked, left, access mode
    changed, session closed or deleted)."""
    if follower is None:
        return

    def allowed() -> bool:
        with database.read() as db:
            return access.credential_for(db, follower.public_id, follower.token) is not None

    async for message in stream(hub, live_channel(follower.public_id), allowed):
        yield ServerSentEvent(event=message.event, data=message.data)


# --- Ballots ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _VoterPoll:
    public_id: str
    assembly_title: str
    poll: Poll
    voted: bool
    accepting: bool
    demo: bool = False  # a demo session (FR-09a): its band on the page of a plain form too


def _voter_poll(
    database: Database, public_id: str, token: str | None, poll_id: int
) -> _VoterPoll | None:
    """The poll as this voter sees it, or None without a valid credential."""
    with database.read() as db:
        credential = access.credential_for(db, public_id, token)
        if credential is None:
            return None
        poll = live.voter_poll(db, credential.assembly_id, poll_id)
        if poll is None:
            raise NotFoundError("poll")
        voted = ballots.has_voted(db, credential, poll.id)
        assembly = db.get_one(Assembly, credential.assembly_id)
        title, demo = assembly.title, assembly.is_demo
    accepting = ballots.is_accepting_votes(poll, utcnow())
    return _VoterPoll(public_id, title, poll, voted, accepting, demo)


def _signed_out(request: Request, public_id: str) -> Response:
    """No valid credential: the script reloads on 401; a plain form goes back to the join page."""
    if _wants_fragment(request):
        return _private(Response(status_code=401))
    return _home(public_id)


def _section(
    request: Request,
    target: _VoterPoll,
    state: str,
    *,
    choice: Choice | None = None,
    ballot_error: str | None = None,
    message: str | None = None,
    status_code: int = 200,
) -> Response:
    """One poll's section: alone for the script, inside a page for plain forms.

    Padded to one size per poll whatever the step and the choice (``padding``): the size of an
    answer must not tell what the voter chose.
    """
    fragment = _wants_fragment(request)
    section = padded_section(
        target.public_id,
        request.state.csrf_token,
        target.poll,
        state,
        choice=choice,
        error=ballot_error,
        message=message,
        focus=fragment,
    )
    context: dict[str, Any] = {"public_id": target.public_id, "section": section}
    if fragment:
        name = "live/_section.html"
    else:
        name = "live/poll.html"
        context.update(
            poll=target.poll, assembly_title=target.assembly_title, demo_session=target.demo
        )
    return _private(render(request, name, context, status_code=status_code))


def _already_voted(request: Request, target: _VoterPoll) -> Response:
    """FR-45: "vote already recorded"."""
    if _wants_fragment(request):
        message = error_message("already_voted")
        return _section(request, target, "voted", message=message, status_code=409)
    response = _home(target.public_id)
    flash(response, request, "live_already_voted")
    return response


def _not_votable(request: Request, target: _VoterPoll) -> Response | None:
    if target.voted:
        return _already_voted(request, target)
    if not target.accepting:
        return _section(
            request, target, "closed", message=error_message("poll_not_open"), status_code=409
        )
    return None


@router.post("/{session_id}/polls/{poll_id}/review", dependencies=[CsrfProtected])
def review(
    request: Request, session_id: str, poll_id: RowId, database: DatabaseDep, form: FormDep
) -> Response:
    """Summary before the final submission (FR-43), or back to the ballot (``action=edit``)."""
    public_id = _public_id(session_id)
    target = _voter_poll(database, public_id, _voter_token(request), poll_id)
    if target is None:
        return _signed_out(request, public_id)
    refused = _not_votable(request, target)
    if refused is not None:
        return refused

    choice = None
    try:
        choice = parse_choice(target.poll, form)
        if form.get("action") == "edit":
            return _section(request, target, "ballot", choice=choice)
        check_choice(target.poll, choice)
    except InvalidInputError as error:
        if form.get("action") == "edit":
            return _section(request, target, "ballot")
        return _section(
            request,
            target,
            "ballot",
            choice=choice,
            ballot_error=error_message(error),
            status_code=422,
        )
    return _section(request, target, "review", choice=choice)


@router.post("/{session_id}/polls/{poll_id}", dependencies=[CsrfProtected])
def cast(
    request: Request,
    session_id: str,
    poll_id: RowId,
    database: DatabaseDep,
    hub: HubDep,
    form: FormDep,
) -> Response:
    """Final submission: participation and ballot in one write transaction (FR-45)."""
    public_id = _public_id(session_id)
    token = _voter_token(request)
    target = _voter_poll(database, public_id, token, poll_id)
    if target is None:
        return _signed_out(request, public_id)
    refused = _not_votable(request, target)
    if refused is not None:
        return refused

    choice = None
    try:
        choice = parse_choice(target.poll, form)
        refuse_blank_with_choice(choice)
        with database.write() as db:
            ballots.cast_vote(db, public_id, token, poll_id, choice.content)
    except AlreadyVotedError:
        return _already_voted(request, target)
    except AccessDeniedError:
        return _signed_out(request, public_id)
    except InvalidStateError as error:
        return _section(request, target, "closed", message=error_message(error), status_code=409)
    except InvalidInputError as error:
        return _section(
            request,
            target,
            "ballot",
            choice=choice,
            ballot_error=error_message(error),
            status_code=422,
        )

    hub.publish(manage_channel(public_id), "turnout")
    if _wants_fragment(request):
        # The confirmation's heading ("Vote enregistré") takes the focus: no sentence repeats it.
        return _section(request, target, "voted")
    response = _home(public_id)
    flash(response, request, "live_vote_recorded")
    return response
