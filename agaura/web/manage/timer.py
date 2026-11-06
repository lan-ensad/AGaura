"""Chronometer and countdown of the projection screen, run from the console (FR-74), often
from a phone.

Stopped at zero, the card offers one setup form (``mode`` ``countdown`` or ``up``, a preset
``duration`` in seconds or ``custom`` with ``minutes`` and ``seconds``, ``label``, ``show``) and
the only start button; running or paused, its controls. The card lives in the Projection screen
rubric; during the session Steering also has its command (D-A), with the same controls. Every
action follows POST/redirect/GET through ``run_action``, redirecting without an anchor: each
form posts to the copy it came from (``…#timer``, ``…#cmd-timer``). The console event it
publishes makes an open projection screen refresh within a second. ``GET …/timer`` returns
both copies alone (``_timer_alone.html``): ``static/js/timer.js`` refetches them when the live
panel refreshes (that is, on every console event), so a console open on another device follows
too.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from agaura.i18n import N_
from agaura.models import Assembly, utcnow
from agaura.security import Keyring
from agaura.services import assemblies, timer
from agaura.services.errors import InvalidInputError
from agaura.web.deps import CsrfProtected, DatabaseDep
from agaura.web.manage.common import (
    ConsoleAccess,
    ConsoleUser,
    CurrentAccess,
    TimerSession,
    console_context,
    router,
    run_action,
)
from agaura.web.messages import ERRORS, NOTICES
from agaura.web.templating import templates

PRESETS = (60, 120, 180, 300)  # seconds: durations of a countdown chosen in one tap
DEFAULT_PRESET = 180
MAX_FIELD_DIGITS = 6  # minutes or seconds typed for a countdown
ANCHOR = ""  # the card and the command each post to the address to come back to

ERRORS.update(
    {
        "timer_invalid_duration": N_("Choose a duration between 1 second and 10 hours."),
        "timer_invalid_mode": N_("Choose a countdown or a chronometer."),
        "timer_counting_up": N_("Only a countdown has a duration to extend."),
    }
)
NOTICES.update(
    {
        "timer_started_countdown": N_("Countdown started."),
        "timer_started_up": N_("Chronometer started."),
        "timer_paused": N_("Paused: the time is stopped."),
        "timer_resumed": N_("The time runs again."),
        "timer_reset": N_("Reset: set up the next one."),
        "timer_extended": N_("One minute added."),
        "timer_shown": N_("Shown on the screen."),
        "timer_hidden": N_("Hidden from the screen."),
    }
)


def custom_duration(minutes: str, seconds: str) -> int:
    """Seconds from the custom fields; both empty or zero is refused."""
    total = 0
    for raw, unit, maximum in ((minutes, 60, None), (seconds, 1, 59)):
        raw = raw.strip()
        if not raw:
            continue
        # ``isdigit`` accepts superscripts and other scripts' digits that ``int`` refuses; past
        # 4,300 digits ``int`` raises, far past what the service accepts (``MAX_DURATION``).
        if (
            not (raw.isascii() and raw.isdigit())
            or len(raw) > MAX_FIELD_DIGITS
            or (maximum is not None and int(raw) > maximum)
        ):
            raise InvalidInputError("timer_invalid_duration", "duration")
        total += int(raw) * unit
    if total == 0:
        raise InvalidInputError("timer_invalid_duration", "duration")
    return total


def chosen_duration(mode: str, duration: str, minutes: str, seconds: str) -> int | None:
    """None for a chronometer (``mode`` ``up``); for a countdown, ``duration`` in seconds, or
    ``custom`` for the fields. The radio decides: custom fields filled while a preset is
    checked are ignored, as the form shows."""
    if mode == "up":
        return None
    if mode != "countdown":
        raise InvalidInputError("timer_invalid_mode", "mode")
    if duration == "custom":
        return custom_duration(minutes, seconds)
    if duration.isascii() and duration.isdigit():
        return int(duration)
    raise InvalidInputError("timer_invalid_duration", "duration")


def card_context(assembly: Assembly) -> dict[str, Any]:
    """What ``manage/_timer.html`` needs besides ``assembly`` and ``csrf_token``; the server's
    time lets the script correct a phone's clock."""
    now = utcnow()
    return {
        "timer_state": timer.state(assembly, now=now),
        "timer_presets": PRESETS,
        "timer_default": DEFAULT_PRESET,
        "timer_now_ms": int(now.timestamp() * 1000),
    }


@console_context
def timer_context(
    request: Request, db: Session, access: ConsoleAccess, assembly: Assembly, keyring: Keyring
) -> dict[str, Any]:
    """Every delegate gets it: the status bar shows the running chronometer to all."""
    return card_context(assembly)


@router.get("/manage/{public_id}/timer", response_class=HTMLResponse)
def timer_card(
    request: Request,
    public_id: TimerSession,
    user: ConsoleUser,
    access: CurrentAccess,
    database: DatabaseDep,
) -> Response:
    """The console card alone, and the command of Steering during the session, for
    ``timer.js``. Not ``render()``: it would consume the flash notice meant for the console
    page. ``can`` gives their buttons the level they have on the page, so the script replaces
    nothing when nothing changed."""
    with database.read() as db:
        assembly = assemblies.get_assembly(db, user, public_id)
        context = {"assembly": assembly, "csrf_token": request.state.csrf_token, "can": access.can}
        context.update(card_context(assembly))
        response = templates.TemplateResponse(request, "manage/_timer_alone.html", context)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/manage/{public_id}/timer/start", dependencies=[CsrfProtected])
def start_timer(
    request: Request,
    public_id: TimerSession,
    user: ConsoleUser,
    mode: Annotated[str, Form()] = "",
    duration: Annotated[str, Form()] = "",
    minutes: Annotated[str, Form()] = "",
    seconds: Annotated[str, Form()] = "",
    label: Annotated[str, Form()] = "",
    show: Annotated[str, Form()] = "",
) -> Response:
    def action(db: Session) -> None:
        chosen = chosen_duration(mode, duration, minutes, seconds)
        timer.start(db, user, public_id, duration=chosen, label=label, show=show == "1")

    notice = "timer_started_up" if mode == "up" else "timer_started_countdown"
    return run_action(request, public_id, action, notice=notice, anchor=ANCHOR)


@router.post("/manage/{public_id}/timer/pause", dependencies=[CsrfProtected])
def pause_timer(request: Request, public_id: TimerSession, user: ConsoleUser) -> Response:
    return run_action(
        request,
        public_id,
        lambda db: timer.pause(db, user, public_id),
        notice="timer_paused",
        anchor=ANCHOR,
    )


@router.post("/manage/{public_id}/timer/resume", dependencies=[CsrfProtected])
def resume_timer(request: Request, public_id: TimerSession, user: ConsoleUser) -> Response:
    return run_action(
        request,
        public_id,
        lambda db: timer.resume(db, user, public_id),
        notice="timer_resumed",
        anchor=ANCHOR,
    )


@router.post("/manage/{public_id}/timer/reset", dependencies=[CsrfProtected])
def reset_timer(request: Request, public_id: TimerSession, user: ConsoleUser) -> Response:
    return run_action(
        request,
        public_id,
        lambda db: timer.reset(db, user, public_id),
        notice="timer_reset",
        anchor=ANCHOR,
    )


@router.post("/manage/{public_id}/timer/add", dependencies=[CsrfProtected])
def add_minute(request: Request, public_id: TimerSession, user: ConsoleUser) -> Response:
    return run_action(
        request,
        public_id,
        lambda db: timer.add_time(db, user, public_id),
        notice="timer_extended",
        anchor=ANCHOR,
    )


@router.post("/manage/{public_id}/timer/show", dependencies=[CsrfProtected])
def show_timer(
    request: Request,
    public_id: TimerSession,
    user: ConsoleUser,
    shown: Annotated[str, Form()] = "",
) -> Response:
    visible = shown == "1"
    return run_action(
        request,
        public_id,
        lambda db: timer.show(db, user, public_id, visible),
        notice="timer_shown" if visible else "timer_hidden",
        anchor=ANCHOR,
    )
