"""Projection screen (FR-70 to FR-75): ``/{session_id}/screen/{token}``, without an account.

The secret link is the only credential: every request checks the token (constant time) and a
wrong, regenerated or deleted link answers the 404 page. The page refreshes its content
through ``/state`` on every event of the console channel (console actions, chronometer and
agenda included, joins, ballots, poll timers), with a polling fallback (``static/js/screen.js``).

Responses are never cached and send no ``Referer`` (the link is in the URL). The screen shows
only what the whole room may see, and a result only when the tribune shows it for a closed poll
(``services/screen.py``, FR-75); the session password and its QR code disappear once entries
are locked (FR-27), in codes mode and after the session.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent

from agaura.services import access, assemblies, screen
from agaura.services.errors import NotFoundError
from agaura.web.deps import DatabaseDep, HubDep, KeyringDep
from agaura.web.manage.common import qr_svg, voter_url
from agaura.web.realtime import Holder, manage_channel, stream
from agaura.web.templating import templates

router = APIRouter()

SERVER_TIME_HEADER = "X-Server-Time"


def _public_id(session_id: str) -> str:
    public_id = assemblies.normalize_public_id(session_id)
    if public_id is None:
        raise NotFoundError("assembly")
    return public_id


def _secret_link(response: Response) -> Response:
    """Behind a secret link: never cached, never indexed, never sent as ``Referer``."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _render(request: Request, name: str, context: dict[str, Any]) -> Response:
    """Render without ``templating.render``, which consumes the flash cookie: a screen open in
    the console's browser must not swallow the console's notices."""
    return templates.TemplateResponse(request, name, context)


def _server_time() -> str:
    """Milliseconds since the epoch: countdowns follow the server's clock, not the projector's."""
    return str(int(time.time() * 1000))


def _context(request: Request, view: screen.ScreenView, token: str) -> dict[str, Any]:
    public_id = view.assembly.public_id
    url = voter_url(request, public_id)
    join_qr = None
    if view.password is not None:
        join_qr = qr_svg(f"{url}#k={quote(view.password, safe='')}")
    # Shown in large type: no "https://", which phones add by themselves.
    address = url.removeprefix("https://")
    return {
        "view": view,
        "public_id": public_id,
        "address_base": address.removesuffix(public_id),
        "join_qr": join_qr,
        "screen_url": f"/{public_id}/screen/{token}",
    }


@router.get("/{session_id}/screen/{token}", response_class=HTMLResponse)
def screen_page(
    request: Request, session_id: str, token: str, database: DatabaseDep, keyring: KeyringDep
) -> Response:
    public_id = _public_id(session_id)
    with database.read() as db:
        assembly = access.check_link_token(db, keyring, public_id, token)
        if session_id != assembly.public_id:
            redirect = RedirectResponse(f"/{assembly.public_id}/screen/{token}", status_code=303)
            return _secret_link(redirect)
        context = _context(request, screen.screen_view(db, keyring, assembly), token)
        context["server_time"] = _server_time()
        return _secret_link(_render(request, "screen/page.html", context))


@router.get("/{session_id}/screen/{token}/state", response_class=HTMLResponse)
def screen_state(
    request: Request, session_id: str, token: str, database: DatabaseDep, keyring: KeyringDep
) -> Response:
    """The screen's content, for the script's refreshes; 404 makes the page reload."""
    public_id = _public_id(session_id)
    with database.read() as db:
        assembly = access.check_link_token(db, keyring, public_id, token)
        context = _context(request, screen.screen_view(db, keyring, assembly), token)
        response = _render(request, "screen/_state.html", context)
    response.headers[SERVER_TIME_HEADER] = _server_time()
    return _secret_link(response)


def _events_access(session_id: str, token: str, database: DatabaseDep, keyring: KeyringDep) -> str:
    """The session ID behind a valid screen link, else the 404 page.

    ``EventSource`` does not reconnect after a 404; the script then refreshes the content,
    which answers 404 too, and reloads the page.
    """
    public_id = _public_id(session_id)
    with database.read() as db:
        return access.check_link_token(db, keyring, public_id, token).public_id


async def _events_slot(
    hub: HubDep, response: Response, public_id: Annotated[str, Depends(_events_access)]
) -> AsyncIterator[str | None]:
    """One of the few streams a screen link may keep open (``realtime.STREAM_CAPS``), else
    429: screen.js then polls and tries again later."""
    with hub.hold(manage_channel(public_id), Holder("screen", (public_id,))) as held:
        if not held:
            response.status_code = 429
        yield public_id if held else None


@router.get("/{session_id}/screen/{token}/events", response_class=EventSourceResponse)
async def screen_events(
    token: str,
    database: DatabaseDep,
    keyring: KeyringDep,
    hub: HubDep,
    public_id: Annotated[str | None, Depends(_events_slot)],
) -> AsyncIterator[ServerSentEvent]:
    """Ends once the link no longer opens the screen (regenerated, session deleted)."""
    if public_id is None:
        return

    def allowed() -> bool:
        with database.read() as db:
            try:
                access.check_link_token(db, keyring, public_id, token)
            except NotFoundError:
                return False
        return True

    async for message in stream(hub, manage_channel(public_id), allowed):
        yield ServerSentEvent(event=message.event, data=message.data)
