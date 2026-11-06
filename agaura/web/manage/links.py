"""Secret links of a session: projection screen (FR-16) and public results (FR-17).

Both links are stored encrypted, so the console can show them again. Regenerating the screen
link makes an open screen stop working at its next refresh, which the console channel triggers
right away. Next to the screen link, "Ce que la salle voit" (``_screen_recap.html``) says in
words what the screen shows, from the same read model as the screen (``links_screen_view``),
with a button on each block to hide or show it (``show_on_screen``); its list is a live part of
the console, rendered again by the panel on every console event (``recap_context``). The links
are the owner's only; the card and its buttons are the owner's and those of a delegate link
holding the right ``screen`` (FR-92).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Form, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from agaura.i18n import N_
from agaura.models import Assembly
from agaura.security import Keyring
from agaura.services import access, polls, screen, timer
from agaura.services.delegates import Right
from agaura.services.errors import InvalidInputError
from agaura.web.deps import CsrfProtected, KeyringDep, public_base_url
from agaura.web.manage.common import (
    ACTIONS,
    ConsoleAccess,
    ConsoleUser,
    OwnedSession,
    ScreenSession,
    console_context,
    optional_int,
    owned_poll,
    router,
    run_action,
)
from agaura.web.messages import ERRORS, NOTICES

ANCHOR = "#screen-now"

ERRORS.update({"screen_unknown_part": N_("This part of the screen cannot be shown or hidden.")})
ACTIONS.update(
    {
        "screen.agenda_shown": N_("Agenda shown on the screen"),
        "screen.agenda_hidden": N_("Agenda hidden from the screen"),
        "screen.join_shown": N_("How to join shown on the screen"),
        "screen.join_hidden": N_("How to join hidden from the screen"),
    }
)
NOTICES.update(
    {
        "screen_part_shown": N_("Shown on the screen."),
        "screen_part_hidden": N_("Hidden from the screen."),
        "screen_link_regenerated": N_("New screen link created: the previous one no longer works."),
        "screen_public_results_enabled": N_("Public results link enabled."),
        "screen_public_results_disabled": N_("Public results link disabled: it no longer works."),
    }
)


def screen_path(public_id: str, token: str) -> str:
    return f"/{public_id}/screen/{token}"


def public_results_path(public_id: str, token: str) -> str:
    return f"/{public_id}/results/{token}"


def recap_context(
    db: Session, keyring: Keyring, console: ConsoleAccess, assembly: Assembly
) -> dict[str, Any]:
    """What the list of "Ce que la salle voit" needs (``_screen_recap_list.html``), for the
    console page and its live panel: the screen's own ``ScreenView`` for the owner and a
    delegate holding the right ``screen``, else None (no card)."""
    allowed = console.is_owner or console.allows(Right.SCREEN)
    return {
        "links_screen_view": screen.screen_view(db, keyring, assembly) if allowed else None,
        "polls_screen_detail_min": screen.SCREEN_DETAIL_MIN,
    }


@console_context
def links_context(
    request: Request, db: Session, console: ConsoleAccess, assembly: Assembly, keyring: Keyring
) -> dict[str, Any]:
    """The secret links, decrypted for the owner only: a delegate never sees them; and what the
    screen shows now (``recap_context``), which the live panel renders again on every console
    event."""
    if not console.is_owner:
        return {"links_screen_url": None, "links_results_url": None} | recap_context(
            db, keyring, console, assembly
        )
    base = public_base_url(request)
    public_id = assembly.public_id
    screen_token = access.screen_token(db, keyring, console.user, public_id)
    results_token = access.public_results_token(db, keyring, console.user, public_id)
    return {
        "links_screen_url": base + screen_path(public_id, screen_token),
        "links_results_url": (
            None if results_token is None else base + public_results_path(public_id, results_token)
        ),
    } | recap_context(db, keyring, console, assembly)


@router.post("/manage/{public_id}/screen-link", dependencies=[CsrfProtected])
def regenerate_screen_link(
    request: Request, public_id: OwnedSession, user: ConsoleUser, keyring: KeyringDep
) -> Response:
    return run_action(
        request,
        public_id,
        lambda db: access.regenerate_screen_token(db, keyring, user, public_id),
        notice="screen_link_regenerated",
        anchor="#links",
    )


@router.post("/manage/{public_id}/public-results", dependencies=[CsrfProtected])
def toggle_public_results(
    request: Request,
    public_id: OwnedSession,
    user: ConsoleUser,
    keyring: KeyringDep,
    enabled: Annotated[str, Form()] = "",
) -> Response:
    enable = enabled == "1"

    def action(db: Session) -> None:
        current = access.public_results_token(db, keyring, user, public_id)
        # Enabling twice (two tabs, a double click) must not replace a link already shared.
        if enable != (current is not None):
            access.set_public_results(db, keyring, user, public_id, enable)

    return run_action(
        request,
        public_id,
        action,
        notice="screen_public_results_enabled" if enable else "screen_public_results_disabled",
        anchor="#public-results",
    )


@router.post("/manage/{public_id}/screen/show", dependencies=[CsrfProtected])
def show_on_screen(
    request: Request,
    public_id: ScreenSession,
    user: ConsoleUser,
    part: Annotated[str, Form()] = "",
    shown: Annotated[str, Form()] = "",
    poll: Annotated[str, Form()] = "",
) -> Response:
    """A block of the projection screen shown (``shown=1``) or hidden, from "Ce que la salle
    voit": the agenda or the way to join (``screen.show_part``), the chronometer
    (``timer.show``), or the result of the closed poll ``poll`` (``polls.show_on_screen``,
    FR-75). The screen follows the console channel that ``run_action`` notifies (FR-73), and so
    does the card: sent by manage.js, the form gets 204 and the live parts are fetched again."""
    visible = shown == "1"

    def action(db: Session) -> None:
        if part == "timer":
            timer.show(db, user, public_id, visible)
        elif part == "result":
            poll_id = optional_int(poll, minimum=1)
            if poll_id is None:
                raise InvalidInputError("screen_unknown_part", "poll")
            polls.show_on_screen(db, user, owned_poll(db, user, public_id, poll_id).id, visible)
        else:
            screen.show_part(db, user, public_id, part, visible)

    return run_action(
        request,
        public_id,
        action,
        notice="screen_part_shown" if visible else "screen_part_hidden",
        anchor=ANCHOR,
        in_place=True,
    )
