"""Polls: the trial vote of the checklist (D-I), live control (FR-32 to FR-36) and results on the
projection screen (FR-75). Creating and editing a poll, "the method first", is editor.py's.

Publishing a result and showing it on the screen are offered by the finished poll in Polls and
by the "Vote clos" command of Steering (D-K): those two routes redirect without an anchor, and
each form posts to the address to come back to (``…#poll-<id>``, ``…#tab-cockpit``). Closing a
poll redirects without one too, so a poll closed from Steering comes back to that command."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Form, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from agaura.i18n import N_, _
from agaura.models import Assembly
from agaura.security import Keyring
from agaura.services import polls, screen
from agaura.services.polls import PollInput
from agaura.web.deps import CsrfProtected, RowId
from agaura.web.manage.common import (
    ACTIONS,
    ConsoleAccess,
    ConsoleUser,
    OwnedSession,
    PollsSession,
    console_context,
    optional_int,
    poll_action,
    router,
    run_action,
)
from agaura.web.messages import ERRORS, NOTICES

ERRORS.update(
    {
        "poll_trial_waiting": N_(
            "A trial vote is already waiting in the drafts: push it once the session is open."
        ),
    }
)
NOTICES.update(
    {
        "poll_shown_on_screen": N_("Result shown on the screen."),
        "poll_taken_off_screen": N_("Result taken off the screen."),
        "poll_trial_created": N_(
            "Trial vote created in the drafts: push it first, once the session is open, to "
            "check that every phone receives it. The minutes and the exports leave it out."
        ),
    }
)
ACTIONS.update(
    {
        "poll.screen_shown": N_("Result shown on the screen"),
        "poll.screen_hidden": N_("Result taken off the screen"),
    }
)


@console_context
def polls_context(
    request: Request, db: Session, console: ConsoleAccess, assembly: Assembly, keyring: Keyring
) -> dict[str, Any]:
    """Ballots from which the projection screen shows the count of each option (FR-75): the
    finished polls say when the screen will give the verdict alone."""
    return {"polls_screen_detail_min": screen.SCREEN_DETAIL_MIN}


def trial_poll() -> PollInput:
    """The trial vote of the checklist (D-I): a yes or no question that tests the room."""
    return PollInput(
        title=_("Is everyone connected?"),
        method="single_choice",
        options=[_("Yes"), _("No")],
        params={"threshold": "plurality"},
    )


@router.post("/manage/{public_id}/polls/trial", dependencies=[CsrfProtected])
def create_trial(request: Request, public_id: OwnedSession, user: ConsoleUser) -> Response:
    """From the checklist at the head of Meeting before the opening, the owner's: one trial
    draft at a time. Back to the checklist, whose line then leads to the draft."""
    return run_action(
        request,
        public_id,
        lambda db: polls.create_trial_poll(db, user, public_id, trial_poll()),
        notice="poll_trial_created",
        anchor="#checklist",
    )


@router.post("/manage/{public_id}/polls/{poll_id}/open", dependencies=[CsrfProtected])
def open_poll(
    request: Request,
    public_id: PollsSession,
    user: ConsoleUser,
    poll_id: RowId,
    minutes: Annotated[str, Form()] = "",
) -> Response:
    return poll_action(
        request,
        public_id,
        user,
        poll_id,
        lambda db, poll: polls.open_poll(
            db, user, poll.id, minutes=optional_int(minutes, minimum=1)
        ),
        notice="poll_opened",
        voters="polls",
        anchor="",
    )


@router.post("/manage/{public_id}/polls/{poll_id}/close", dependencies=[CsrfProtected])
def close_poll(
    request: Request, public_id: PollsSession, user: ConsoleUser, poll_id: RowId
) -> Response:
    return poll_action(
        request,
        public_id,
        user,
        poll_id,
        lambda db, poll: polls.close_poll(db, user, poll.id),
        notice="poll_closed",
        voters="polls",
        # Back where it was closed: Steering, which then heads with the "Vote clos" command
        # (D-K), or Polls (manage.js adds the rubric; the form is the same in both places).
        anchor="",
    )


@router.post("/manage/{public_id}/polls/{poll_id}/cancel", dependencies=[CsrfProtected])
def cancel_poll(
    request: Request,
    public_id: PollsSession,
    user: ConsoleUser,
    poll_id: RowId,
    reason: Annotated[str, Form()] = "",
) -> Response:
    return poll_action(
        request,
        public_id,
        user,
        poll_id,
        lambda db, poll: polls.cancel_poll(db, user, poll.id, reason),
        notice="poll_cancelled",
        voters="polls",
        anchor="#results",
    )


@router.post("/manage/{public_id}/polls/{poll_id}/publish", dependencies=[CsrfProtected])
def publish_poll(
    request: Request,
    public_id: PollsSession,
    user: ConsoleUser,
    poll_id: RowId,
    voters: Annotated[str, Form()] = "",
) -> Response:
    """Publish to voters (FR-36); the projection screen is a separate choice (FR-75)."""
    return poll_action(
        request,
        public_id,
        user,
        poll_id,
        lambda db, poll: polls.publish_result(db, user, poll.id, voters=voters == "1"),
        notice="poll_published",
        voters="results",
        anchor="",
    )


@router.post("/manage/{public_id}/polls/{poll_id}/dismiss", dependencies=[CsrfProtected])
def dismiss_poll(
    request: Request, public_id: PollsSession, user: ConsoleUser, poll_id: RowId
) -> Response:
    return poll_action(
        request,
        public_id,
        user,
        poll_id,
        lambda db, poll: polls.dismiss_result(db, user, poll.id),
        notice="poll_dismissed",
        voters="results",
        anchor="#results",
    )


@router.post("/manage/{public_id}/polls/{poll_id}/screen", dependencies=[CsrfProtected])
def show_on_screen(
    request: Request,
    public_id: PollsSession,
    user: ConsoleUser,
    poll_id: RowId,
    shown: Annotated[str, Form()] = "",
) -> Response:
    """``shown=1``: show the result of a closed poll on the projection screen; ``shown=0``:
    take it off (FR-75). The screen follows the console channel that ``run_action`` notifies,
    so it changes within a second (FR-73); voter pages are untouched."""
    show = shown == "1"
    return poll_action(
        request,
        public_id,
        user,
        poll_id,
        lambda db, poll: polls.show_on_screen(db, user, poll.id, show),
        notice="poll_shown_on_screen" if show else "poll_taken_off_screen",
        anchor="",
    )


@router.post("/manage/{public_id}/polls/{poll_id}/duplicate", dependencies=[CsrfProtected])
def duplicate_poll(
    request: Request, public_id: PollsSession, user: ConsoleUser, poll_id: RowId
) -> Response:
    return poll_action(
        request,
        public_id,
        user,
        poll_id,
        lambda db, poll: polls.duplicate_poll(db, user, poll.id),
        notice="poll_duplicated",
    )


@router.post("/manage/{public_id}/polls/{poll_id}/delete", dependencies=[CsrfProtected])
def delete_poll(
    request: Request, public_id: PollsSession, user: ConsoleUser, poll_id: RowId
) -> Response:
    return poll_action(
        request,
        public_id,
        user,
        poll_id,
        lambda db, poll: polls.delete_poll(db, user, poll.id),
        notice="poll_deleted",
    )
