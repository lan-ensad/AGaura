"""Delegate links (FR-92): part of the console of one session, for someone without an account.

The owner creates a link per person in Settings, with a name and some of six rights (agenda,
session notes, polls, chronometer, what the room sees, headcount), shows it again or as a QR
code, changes its rights, or revokes it. Each card of a right, and its command in Steering
during the session, says who holds it with a pill that leads to the link in Settings, and offers
"Confier": the form of a new link in the card itself, that right ticked (``hand_over`` of
``macros/delegates.html``). Its answer comes back to that card (``?hand=<card>&link=<id>``, read
on the console page, never written), or, for ``manage.js``, is the fresh delegation of every
card and the Settings card, swapped in place. The link is ``/manage/{ID}/delegate#k={token}``:
the token travels in the fragment, which browsers never send to the server, so it never reaches
an access log or a ``Referer`` header. ``delegate.js`` reads it and posts it; the answer keeps
it in an ``HttpOnly`` cookie scoped to ``/manage/{ID}`` and opens the console, limited to the
link's rights (``common.console_access``). Without JavaScript, the key can be pasted into the
form.

The entry routes (open the link, leave) need no account, so they live on ``entry_router``,
outside the console router that the isolation tests walk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from markupsafe import Markup
from sqlalchemy.orm import Session

from agaura.db import Database
from agaura.i18n import N_
from agaura.models import Assembly, AssemblyStatus
from agaura.security import Keyring
from agaura.services import assemblies, delegates
from agaura.services.delegates import MAX_DELEGATES, MAX_NAME_LENGTH, RIGHTS
from agaura.services.errors import NotFoundError, ServiceError
from agaura.web.deps import CsrfProtected, DatabaseDep, KeyringDep, LimiterDep, public_base_url
from agaura.web.limits import WINDOW, limit_address
from agaura.web.manage.common import (
    ACTIONS,
    DELEGATE_COOKIE,
    DELEGATE_COOKIE_AGE,
    ConsoleAccess,
    ConsoleUser,
    CurrentAccess,
    OwnedSession,
    console_context,
    console_write,
    qr_svg,
    router,
    run_action,
)
from agaura.web.messages import ERRORS, NOTICES, error_message
from agaura.web.realtime import manage_channel
from agaura.web.security import delete_cookie, set_cookie
from agaura.web.templating import flash, render, templates

entry_router = APIRouter()

FAILURES_PER_IP = (30, WINDOW)  # wrong keys per address; a key is 256 bits, never guessed
ANCHOR = "#delegates"
NEW_ANCHOR = "#delegates-new"  # the tile of a new link in Settings, shown when it is the target

ERRORS.update(
    {
        "delegates_name_required": N_("Enter the name of the person who receives the link."),
        "delegates_name_too_long": N_("A name has at most 100 characters."),
        "delegates_rights_required": N_("Choose at least one thing this link may do."),
        "delegates_quota": N_("A session has at most 20 delegate links in use."),
        "delegates_wrong_key": N_(
            "This link does not open the console: it was revoked, the session is closed, or the "
            "address is incomplete. Ask the tribune for a new link."
        ),
    }
)
NOTICES.update(
    {
        "delegates_created": N_(
            "Link created. Send it to this person only, or let them scan the QR code."
        ),
        "delegates_saved": N_("Rights of the link changed; the link itself stays the same."),
        "delegates_revoked": N_("Link revoked: it no longer opens the console."),
        "delegates_left": N_("Delegate access removed from this device."),
    }
)
ACTIONS.update(
    {
        "delegate.created": N_("Delegate link created"),
        "delegate.updated": N_("Delegate link changed"),
        "delegate.revoked": N_("Delegate link revoked"),
    }
)


def link_path(public_id: str) -> str:
    return f"/manage/{public_id}/delegate"


def cookie_path(public_id: str) -> str:
    return f"/manage/{public_id}"


@dataclass(frozen=True, slots=True)
class LinkView:
    """One link in use, in the owner's card."""

    id: int
    name: str
    rights: list[str]
    url: str
    qr: Markup
    created_at: datetime
    last_seen_at: datetime | None


@dataclass(frozen=True, slots=True)
class Holder:
    """A link in use that holds a right, for the pill of a card: its name, and its id for the
    pill's link to it in Settings (``#delegate-<id>``)."""

    id: int
    name: str


def holders(links: list[LinkView]) -> dict[str, list[Holder]]:
    """The links in use that hold each right, in the order of the links: the "Handed over to …"
    pill of each console card (``delegate_badge`` of ``macros/delegates.html``). Rights no link
    holds are absent."""
    found: dict[str, list[Holder]] = {}
    for link in links:
        for right in link.rights:
            found.setdefault(right, []).append(Holder(link.id, link.name))
    return found


# The cards and commands that hand a right over in place (``hand_over``): their id, which is
# the anchor the form comes back to, and the right they tick.
HAND_OVER_CARDS = {
    "agenda": "agenda",
    "agenda-now": "agenda",
    "notes": "notes",
    "cmd-notes": "notes",
    "drafts": "polls",
    "ready": "polls",
    "timer": "timer",
    "cmd-timer": "timer",
    "screen-now": "screen",
    "presence": "headcount",
    "cmd-presence": "headcount",
}


def opened_card(request: Request) -> str | None:
    """The card whose "Confier" opens on this page (``?hand=<card>``), when it is one; anything
    else is ignored. A GET writes nothing."""
    raw = request.query_params.get("hand", "")
    return raw if raw in HAND_OVER_CARDS else None


def created_link(request: Request, links: list[LinkView]) -> LinkView | None:
    """The link just created from a card (``?link=<id>``), when it is one in use of this
    session, to show its address and QR code in that card."""
    raw = request.query_params.get("link", "")
    return next((link for link in links if str(link.id) == raw), None)


@console_context
def delegates_context(
    request: Request, db: Session, access: ConsoleAccess, assembly: Assembly, keyring: Keyring
) -> dict[str, Any]:
    """The owner's card in Settings (links in use, with their address and QR code), the
    holders on the pills of the cards handed over, whether a card may offer "Confier"
    (``delegates_hand_over``), the card whose "Confier" opens (``delegates_open``) with the link
    just created there (``delegates_created``), and the names of the session log. A delegate
    gets none of them: it sees its own name only, in the top bar."""
    context: dict[str, Any] = {
        "delegates_links": [],
        "delegates_holders": {},
        "delegates_names": {},
        "delegates_rights": [right.value for right in RIGHTS],
        "delegates_max": MAX_DELEGATES,
        "delegates_name_length": MAX_NAME_LENGTH,
        "delegates_hand_over": False,
        "delegates_open": None,
        "delegates_created": None,
    }
    if not access.is_owner:
        return context
    links = []
    if assembly.status is not AssemblyStatus.CLOSED:
        base = public_base_url(request) + link_path(assembly.public_id)
        for delegate in delegates.list_delegates(db, access.user, assembly.public_id):
            url = f"{base}#k={delegates.link_token(keyring, delegate)}"
            links.append(
                LinkView(
                    id=delegate.id,
                    name=delegate.name,
                    rights=list(delegate.rights),
                    url=url,
                    qr=qr_svg(url),
                    created_at=delegate.created_at,
                    last_seen_at=delegate.last_seen_at,
                )
            )
    context["delegates_links"] = links
    context["delegates_holders"] = holders(links)
    context["delegates_names"] = delegates.names(db, assembly)
    if assembly.status is not AssemblyStatus.CLOSED:
        context["delegates_hand_over"] = len(links) < MAX_DELEGATES
        context["delegates_open"] = opened_card(request)
        if context["delegates_open"]:
            context["delegates_created"] = created_link(request, links)
    return context


def _delegate_id(raw: str) -> int:
    if raw.isascii() and raw.isdigit() and len(raw) <= 12:
        return int(raw)
    raise NotFoundError("delegate")


# --- Owner: create, change, revoke -------------------------------------------------------------


@router.post("/manage/{public_id}/delegates", dependencies=[CsrfProtected])
def create_link(
    request: Request,
    public_id: OwnedSession,
    user: ConsoleUser,
    access: CurrentAccess,
    database: DatabaseDep,
    keyring: KeyringDep,
    name: Annotated[str, Form()] = "",
    rights: Annotated[list[str] | None, Form()] = None,
    card: Annotated[str, Form()] = "",
) -> Response:
    """A name and any of the rights, from Settings or from the "Confier" of a card (``card``,
    one of ``HAND_OVER_CARDS``; anything else counts as Settings). From Settings, the answer
    leads to the new link's address and QR code there (``#delegate-<id>-link``), or back to the
    tile of a new link after an error (``#delegates-new``); from a card, it comes back to that
    card, which shows them (``?hand=<card>&link=<id>#<card>``), or the form again after an
    error. With ``X-Requested-With: fetch`` (manage.js), the answer is the fresh
    delegation of every card and the Settings card, or the error message with status 400."""
    origin = card if card in HAND_OVER_CARDS else None
    if origin and _from_script(request):
        return _create_in_place(request, public_id, access, database, keyring, name, rights, origin)
    created: list[int] = []

    def action(db: Session) -> None:
        delegate, _token = delegates.create_delegate(
            db, keyring, user, public_id, name, rights or []
        )
        created.append(delegate.id)

    anchor = f"?hand={origin}#{origin}" if origin else NEW_ANCHOR
    response = run_action(request, public_id, action, notice="delegates_created", anchor=anchor)
    if created:
        place = (
            f"?hand={origin}&link={created[0]}#{origin}"
            if origin
            else f"#delegate-{created[0]}-link"
        )
        response.headers["location"] = f"/manage/{public_id}{place}"
    return response


def _from_script(request: Request) -> bool:
    return request.headers.get("x-requested-with") == "fetch"


def _create_in_place(
    request: Request,
    public_id: str,
    access: ConsoleAccess,
    database: Database,
    keyring: Keyring,
    name: str,
    rights: list[str] | None,
    card: str,
) -> Response:
    """``create_link`` for ``manage.js``: the same write, the console event, then every
    ``hand_over`` slot of the page (the one of ``card`` open on the new link) and the Settings
    card, to swap in place. Not ``render()``: no flash notice is consumed or set."""
    try:
        with console_write(request) as db:
            delegate, _token = delegates.create_delegate(
                db, keyring, access.user, public_id, name, rights or []
            )
            created = delegate.id
    except NotFoundError:
        raise
    except ServiceError as error:
        return PlainTextResponse(error_message(error), status_code=400)
    request.app.state.hub.publish(manage_channel(public_id), "turnout", "console")
    with database.read() as db:
        assembly = assemblies.get_assembly(db, access.user, public_id)
        context: dict[str, Any] = {
            "assembly": assembly,
            "csrf_token": request.state.csrf_token,
            "can": access.can,
            "delegates_cards": HAND_OVER_CARDS,
        }
        context.update(delegates_context(request, db, access, assembly, keyring))
        links = context["delegates_links"]
        context["delegates_open"] = card
        context["delegates_created"] = next(link for link in links if link.id == created)
        response = templates.TemplateResponse(request, "manage/_hand_over.html", context)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/manage/{public_id}/delegates/edit", dependencies=[CsrfProtected])
def edit_link(
    request: Request,
    public_id: OwnedSession,
    user: ConsoleUser,
    delegate: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    rights: Annotated[list[str] | None, Form()] = None,
) -> Response:
    """Rename a link or change its rights. Like every console action, it tells the consoles
    of the session to refresh: a delegate's console that lost a right redraws itself."""
    delegate_id = _delegate_id(delegate)
    return run_action(
        request,
        public_id,
        lambda db: delegates.update_delegate(db, user, public_id, delegate_id, name, rights or []),
        notice="delegates_saved",
        anchor=f"#delegate-{delegate_id}",
    )


@router.post("/manage/{public_id}/delegates/revoke", dependencies=[CsrfProtected])
def revoke_link(
    request: Request,
    public_id: OwnedSession,
    user: ConsoleUser,
    delegate: Annotated[str, Form()] = "",
) -> Response:
    """End a link: the consoles it opened refetch at once (console event) and show the page
    of an ended link."""
    delegate_id = _delegate_id(delegate)
    return run_action(
        request,
        public_id,
        lambda db: delegates.revoke_delegate(db, user, public_id, delegate_id),
        notice="delegates_revoked",
        anchor=ANCHOR,
    )


# --- Entry: open the link, leave (no account) ---------------------------------------------------


def _entry_page(
    request: Request, public_id: str, *, error: str | None = None, status_code: int = 200
) -> Response:
    context = {"public_id": public_id, "error": error}
    response = render(request, "manage/delegate_join.html", context, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex"
    return response


def _canonical(public_id: str) -> str:
    normalized = assemblies.normalize_public_id(public_id)
    if normalized is None:
        raise NotFoundError("assembly")
    return normalized


@entry_router.get("/manage/{public_id}/delegate", response_class=HTMLResponse)
def open_link(request: Request, public_id: str) -> Response:
    """The page behind the link: ``delegate.js`` sends the key of the fragment at once."""
    return _entry_page(request, _canonical(public_id))


@entry_router.post("/manage/{public_id}/delegate", dependencies=[CsrfProtected])
def use_link(
    request: Request,
    public_id: str,
    database: DatabaseDep,
    limiter: LimiterDep,
    key: Annotated[str, Form()] = "",
) -> Response:
    """Check the key, keep it in the cookie of this device, open the console."""
    canonical = _canonical(public_id)
    address_key = f"delegate-ip:{limit_address(request)}"
    if limiter.exceeded(address_key, *FAILURES_PER_IP):
        message = error_message("too_many_attempts")
        return _entry_page(request, canonical, error=message, status_code=429)
    token = "".join(key.split())
    with database.read() as db:
        found = delegates.authenticate(db, canonical, token)
    if found is None:
        limiter.record(address_key)
        return _entry_page(
            request, canonical, error=error_message("delegates_wrong_key"), status_code=403
        )
    response = RedirectResponse(f"/manage/{canonical}", status_code=303)
    set_cookie(
        response,
        request,
        DELEGATE_COOKIE,
        token,
        max_age=DELEGATE_COOKIE_AGE,
        path=cookie_path(canonical),
    )
    return response


@entry_router.post("/manage/{public_id}/delegate/leave", dependencies=[CsrfProtected])
def leave(request: Request, public_id: str) -> Response:
    """Forget the link on this device (shared computer); the link itself keeps working."""
    response = RedirectResponse("/", status_code=303)
    delete_cookie(response, request, DELEGATE_COOKIE, path=cookie_path(_canonical(public_id)))
    flash(response, request, "delegates_left")
    return response
