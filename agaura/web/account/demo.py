"""Demo (FR-09a): ``/demo`` explains it and opens an account of one's own after the anti-robot
check; logging out of a demo account deletes it (``agaura.web.public.logout``).

On unless the operator turns it off (``AGAURA_DEMO=false``); then ``/demo`` is the 404 page.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from agaura.i18n import N_
from agaura.services import auth, demo
from agaura.services.auth import DEMO_SESSION_MAX_AGE
from agaura.services.errors import InvalidStateError, NotFoundError
from agaura.web import pow as proof_of_work
from agaura.web.deps import (
    SESSION_COOKIE,
    CsrfProtected,
    DatabaseDep,
    KeyringDep,
    LimiterDep,
    OptionalUser,
    SettingsDep,
)
from agaura.web.limits import limit_address
from agaura.web.messages import ERRORS, NOTICES, error_message
from agaura.web.realtime import announce_deleted
from agaura.web.security import read_cookie, set_cookie
from agaura.web.templating import flash, render

router = APIRouter()

# (limit, window in seconds): each demo writes two sessions and 96 simulated ballots.
DEMOS_PER_IP = (5, 3600.0)
DEMO_COOKIE_MAX_AGE = int(DEMO_SESSION_MAX_AGE.total_seconds())

ERRORS.update(
    {
        "demo_full": N_(
            "Too many demonstrations are open at the moment. Try again in a few minutes."
        ),
    }
)
NOTICES.update(
    {
        "demo_started": N_(
            "Demonstration open: two example sessions are ready. Nothing will be kept."
        ),
        "demo_ended": N_("Demonstration ended: its account and its sessions are deleted."),
    }
)


def _demo_page(request: Request, *, error: str | None = None, status_code: int = 200) -> Response:
    context: dict[str, Any] = {"pow_refresh": proof_of_work.PAGE_REFRESH}
    if error:
        context["error"] = error_message(error)
    return render(request, "account/demo.html", context, status_code=status_code)


@router.get("/demo")
def demo_page(request: Request, user: OptionalUser, settings: SettingsDep) -> Response:
    if not settings.demo:
        raise NotFoundError("demo")
    if user is not None and user.is_demo:
        return RedirectResponse("/dashboard", status_code=303)
    return _demo_page(request)


@router.post("/demo", dependencies=[CsrfProtected])
def start_demo(
    request: Request,
    database: DatabaseDep,
    keyring: KeyringDep,
    limiter: LimiterDep,
    settings: SettingsDep,
    altcha: Annotated[str, Form()] = "",
) -> Response:
    if not settings.demo:
        raise NotFoundError("demo")
    ip_key = f"demo-ip:{limit_address(request)}"
    if limiter.exceeded(ip_key, *DEMOS_PER_IP):
        return _demo_page(request, error="too_many_attempts", status_code=429)
    if not proof_of_work.verify(keyring, altcha):
        return _demo_page(request, error="pow_failed", status_code=400)
    if not limiter.charge({ip_key: DEMOS_PER_IP}):  # checked and counted in one step
        return _demo_page(request, error="too_many_attempts", status_code=429)
    try:
        with database.write() as db:
            freed = demo.free_a_place(db, limit=settings.demo_max)
            account = demo.start(db, keyring, limit=settings.demo_max)
            # Whoever was logged in on this browser before is logged out (FR-03).
            auth.end_session(db, read_cookie(request, SESSION_COOKIE))
            token = auth.start_session(db, account, user_agent=request.headers.get("user-agent"))
    except InvalidStateError as error:
        limiter.refund(ip_key)
        return _demo_page(request, error=error.code, status_code=503)
    announce_deleted(request.app.state.hub, freed)
    response = RedirectResponse("/dashboard", status_code=303)
    set_cookie(response, request, SESSION_COOKIE, token, max_age=DEMO_COOKIE_MAX_AGE)
    flash(response, request, "demo_started")
    return response
