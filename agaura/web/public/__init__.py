"""Public pages: home, "join a session" field, login and logout."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from agaura.models import User
from agaura.security import hash_token
from agaura.services import assemblies, auth, demo
from agaura.web import pow as proof_of_work
from agaura.web.deps import (
    SESSION_COOKIE,
    CsrfProtected,
    DatabaseDep,
    KeyringDep,
    LimiterDep,
    OptionalUser,
    landing_page,
)
from agaura.web.limits import limit_address
from agaura.web.messages import error_message
from agaura.web.realtime import announce_deleted
from agaura.web.security import delete_cookie, read_cookie, set_cookie
from agaura.web.templating import flash, render

router = APIRouter()

# Failed logins (SPEC §9.3). Nothing here refuses a right password because of failures made
# from other addresses: knowing someone's email address must not be enough to lock them out.
LOGIN_FAILURES_PER_IP = (60, 600.0)  # every account; generous: mobile carriers share IPs (CGNAT)
LOGIN_FAILURES_PER_PAIR = (10, 900.0)  # one account from one address: refused past it
# One account from every address together: past it, a login on that account needs the anti-robot
# proof of work (``agaura.web.pow``), so guessing from many addresses gets costly while the owner
# still logs in. Counted for unknown addresses too: the check tells nothing about who has an
# account. The owner rarely fails 5 times in 15 minutes; the check costs a browser under a second.
LOGIN_FAILURES_BEFORE_POW = (5, 900.0)
# The login cookie: the browser forgets it once its session can no longer be valid.
COOKIE_MAX_AGE = int(auth.SESSION_MAX_AGE.total_seconds())


# The only pages a super-admin can open; any other ``next`` leads to its accounts page.
SUPERADMIN_PAGES = ("/superadmin", "/account")


def safe_next(target: str | None, default: str = "/dashboard") -> str:
    """Only local paths, to avoid open redirects."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return default
    return target


def after_login(user: User, target: str | None) -> str:
    """Where to go once logged in: ``next`` if the account may open it, else its landing page."""
    landing = landing_page(user)
    target = safe_next(target, landing)
    if user.is_superadmin and not any(
        target == page or target.startswith((f"{page}/", f"{page}?")) for page in SUPERADMIN_PAGES
    ):
        return landing
    return target


@router.get("/", response_class=HTMLResponse)
def home(request: Request, user: OptionalUser) -> HTMLResponse:
    return render(request, "public/home.html")


@router.get("/join")
def join_by_id(request: Request, user: OptionalUser, session: str = "") -> Response:
    """The home page field: go to ``/{session_id}`` from a typed session ID."""
    public_id = assemblies.normalize_public_id(session)
    if public_id is None:
        return render(
            request,
            "public/home.html",
            {"error": error_message("not_found"), "session": session},
            status_code=404,
        )
    return RedirectResponse(f"/{public_id}", status_code=303)


def _login_page(
    request: Request,
    form: dict[str, str],
    *,
    pow_required: bool = False,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    """The login form, with the anti-robot check if ``pow_required``."""
    context: dict[str, Any] = {
        "next": safe_next(form.get("next"), ""),
        "email": form.get("email", ""),
        "pow_required": pow_required,
        "pow_refresh": proof_of_work.PAGE_REFRESH,
    }
    if error:
        context["error"] = error_message(error)
    return render(request, "public/login.html", context, status_code=status_code)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, user: OptionalUser, next: str | None = None) -> Response:
    if user is not None:
        return RedirectResponse(after_login(user, next), status_code=303)
    return _login_page(request, {"next": next or ""})


@router.post("/login", dependencies=[CsrfProtected])
def login(
    request: Request,
    database: DatabaseDep,
    keyring: KeyringDep,
    limiter: LimiterDep,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
    altcha: Annotated[str, Form()] = "",
) -> Response:
    address = limit_address(request)
    typed = hash_token(email.strip().lower())  # bounded key size, no address kept in memory
    ip_key = f"login-ip:{address}"
    pair_key = f"login-pair:{address}:{typed}"
    email_key = f"login-email:{typed}"
    form = {"next": next, "email": email}
    # Counted as a failure from now on, so that concurrent attempts see each other; taken back
    # unless the password turns out wrong.
    if not limiter.charge({ip_key: LOGIN_FAILURES_PER_IP, pair_key: LOGIN_FAILURES_PER_PAIR}):
        return _login_page(request, form, error="too_many_attempts", status_code=429)
    try:
        if limiter.exceeded(email_key, *LOGIN_FAILURES_BEFORE_POW) and not proof_of_work.verify(
            keyring, altcha
        ):
            limiter.refund(ip_key, pair_key)  # no password was checked
            return _login_page(
                request, form, pow_required=True, error="login_pow_required", status_code=400
            )
        with database.read() as db:
            attempt = auth.find_login(db, email)
        attempt = auth.check_login(attempt, password)  # argon2, outside any transaction
        with database.write() as db:
            user = auth.record_login(db, attempt)
            token = None
            if user is not None:
                # Whoever was logged in on this browser before (this account or another one)
                # is logged out: that login session would otherwise live on, out of sight.
                auth.end_session(db, read_cookie(request, SESSION_COOKIE))
                token = auth.start_session(db, user, user_agent=request.headers.get("user-agent"))
    except BaseException:
        limiter.refund(ip_key, pair_key)  # busy server, database error: not a failed guess
        raise
    if user is None or token is None:
        limiter.record(email_key)
        pow_required = limiter.exceeded(email_key, *LOGIN_FAILURES_BEFORE_POW)
        return _login_page(
            request, form, pow_required=pow_required, error="invalid_login", status_code=400
        )
    limiter.refund(ip_key, pair_key)
    response = RedirectResponse(after_login(user, next), status_code=303)
    # The server enforces both expiries (``auth.SESSION_LIFETIME`` idle, ``SESSION_MAX_AGE`` in
    # all); the cookie only lasts as long as the second.
    set_cookie(response, request, SESSION_COOKIE, token, max_age=COOKIE_MAX_AGE)
    flash(response, request, "logged_in")
    return response


@router.post("/logout", dependencies=[CsrfProtected])
def logout(request: Request, database: DatabaseDep, user: OptionalUser) -> Response:
    """Log out; a demo account, which has no password to come back with, is deleted with its
    sessions at once (FR-09a)."""
    ended: list[str] = []
    with database.write() as db:
        auth.end_session(db, read_cookie(request, SESSION_COOKIE))
        if user is not None and user.is_demo:
            ended = demo.end(db, user)
    if ended:
        announce_deleted(request.app.state.hub, ended)
    response = RedirectResponse("/", status_code=303)
    delete_cookie(response, request, SESSION_COOKIE)
    flash(response, request, "demo_ended" if user is not None and user.is_demo else "logged_out")
    return response
