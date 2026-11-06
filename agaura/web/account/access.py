"""Signup with the anti-bot proof of work, email verification and password reset
(FR-01, FR-02, FR-04).

Signup is closed unless ``AGAURA_SIGNUP=open``: the super-admin then creates accounts by
invitation (``agaura/web/account/invitation.py``), ``/signup`` only says so and refuses its form.
The anti-bot challenge is served in both modes: the login form asks for it too, for an address
with many failed logins (``agaura.web.public``).

A taken address at signup gets a neutral refusal, which still tells that it has an account:
signing up logs the new account in at once (FR-01), so no answer can look the same for a taken
address. The proof of work and ``SIGNUPS_PER_IP`` make each probe costly, and signup is closed
by default; the login, reset and email change forms reveal nothing.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from agaura.i18n import _
from agaura.models import EmailTokenKind
from agaura.security import password_slot
from agaura.services import accounts, auth, emails
from agaura.services.errors import InvalidInputError
from agaura.web import pow as proof_of_work
from agaura.web.account.common import (
    LINK_ERRORS,
    RESETS_PER_EMAIL,
    RESETS_PER_IP,
    RESETS_PER_PAIR,
    SIGNUPS_PER_IP,
    email_link,
    invalid_link_page,
    no_referrer,
)
from agaura.web.deps import (
    SESSION_COOKIE,
    CsrfProtected,
    DatabaseDep,
    KeyringDep,
    LimiterDep,
    OptionalUser,
    SettingsDep,
    landing_page,
)
from agaura.web.limits import limit_address
from agaura.web.messages import error_message
from agaura.web.public import COOKIE_MAX_AGE
from agaura.web.security import delete_cookie, read_cookie, set_cookie
from agaura.web.templating import flash, render

router = APIRouter()

# The page solves a new challenge two minutes before the current one expires.
POW_REFRESH_SECONDS = proof_of_work.PAGE_REFRESH


@router.get("/altcha/challenge")
def altcha_challenge(keyring: KeyringDep) -> Response:
    return JSONResponse(
        proof_of_work.create_challenge(keyring), headers={"cache-control": "no-store"}
    )


# --- Signup ---------------------------------------------------------------------------------


def _signup_page(
    request: Request, context: dict[str, Any], *, error: str | None = None, status_code: int = 200
) -> Response:
    full = {
        "email": "",
        "display_name": "",
        "pow_refresh": POW_REFRESH_SECONDS,
        "min_password_length": accounts.MIN_PASSWORD_LENGTH,
        **context,
    }
    if error:
        full["error"] = error_message(error)
    return render(request, "account/signup.html", full, status_code=status_code)


def _signup_closed_page(request: Request, *, status_code: int = 200) -> Response:
    return render(request, "account/signup_closed.html", status_code=status_code)


@router.get("/signup")
def signup_form(request: Request, user: OptionalUser, settings: SettingsDep) -> Response:
    if user is not None:
        return RedirectResponse(landing_page(user), status_code=303)
    if not settings.signup_open:
        return _signup_closed_page(request)
    return _signup_page(request, {})


@router.post("/signup", dependencies=[CsrfProtected])
def signup(
    request: Request,
    database: DatabaseDep,
    keyring: KeyringDep,
    limiter: LimiterDep,
    settings: SettingsDep,
    user: OptionalUser,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_confirm: Annotated[str, Form()] = "",
    display_name: Annotated[str, Form()] = "",
    altcha: Annotated[str, Form()] = "",
) -> Response:
    if not settings.signup_open:
        return _signup_closed_page(request, status_code=403)
    context = {"email": email, "display_name": display_name}
    ip_key = f"signup-ip:{limit_address(request)}"
    if limiter.exceeded(ip_key, *SIGNUPS_PER_IP):
        return _signup_page(request, context, error="too_many_attempts", status_code=429)
    if not proof_of_work.verify(keyring, altcha):
        return _signup_page(request, context, error="pow_failed", status_code=400)
    if not limiter.charge({ip_key: SIGNUPS_PER_IP}):  # checked and counted in one step
        return _signup_page(request, context, error="too_many_attempts", status_code=429)
    if password != password_confirm:
        return _signup_page(request, context, error="password_mismatch", status_code=400)
    try:
        # The hashing turn is taken before the write lock, which votes need (agaura.security).
        with password_slot(), database.write() as db:
            account, token = accounts.register(db, email, password, display_name=display_name)
            emails.queue(
                db,
                account.email,
                emails.verification(account, email_link(request, "verify", token)),
            )
            # Whoever was logged in on this browser before is logged out.
            auth.end_session(db, read_cookie(request, SESSION_COOKIE))
            session_token = auth.start_session(
                db, account, user_agent=request.headers.get("user-agent")
            )
    except InvalidInputError as error:
        # Neutral wording: the form does not say outright that the address has an account.
        code = "email_unavailable" if error.code == "email_taken" else error.code
        return _signup_page(request, context, error=code, status_code=400)
    response = RedirectResponse("/dashboard", status_code=303)
    set_cookie(response, request, SESSION_COOKIE, session_token, max_age=COOKIE_MAX_AGE)
    flash(response, request, "signed_up")
    return response


# --- Email verification ---------------------------------------------------------------------


@router.get("/verify/{token}")
def verify(request: Request, token: str, database: DatabaseDep, user: OptionalUser) -> Response:
    try:
        with database.write() as db:
            account = accounts.verify_email(db, token)
    except InvalidInputError as error:
        return invalid_link_page(
            request,
            error,
            retry_url="/account",
            retry_label=_("Ask for a new link from your account page"),
        )
    same_account = user is not None and user.id == account.id
    response = RedirectResponse(
        "/dashboard" if same_account else "/login?next=/dashboard", status_code=303
    )
    flash(response, request, "email_verified")
    return response


# --- Password reset -------------------------------------------------------------------------


@router.get("/reset")
def reset_form(request: Request, user: OptionalUser) -> Response:
    return render(request, "account/reset_request.html", {"email": ""})


@router.post("/reset", dependencies=[CsrfProtected])
def reset_request(
    request: Request,
    database: DatabaseDep,
    limiter: LimiterDep,
    user: OptionalUser,
    email: Annotated[str, Form()] = "",
) -> Response:
    client = limit_address(request)
    ip_key = f"reset-ip:{client}"
    try:
        address = accounts.normalize_email(email)
    except InvalidInputError as error:
        return render(
            request,
            "account/reset_request.html",
            {"email": email, "error": error_message(error)},
            status_code=400,
        )
    if not limiter.charge({ip_key: RESETS_PER_IP}):  # checked and counted in one step
        return render(
            request,
            "account/reset_request.html",
            {"email": email, "error": error_message("too_many_attempts")},
            status_code=429,
        )
    # Over either limit of the email address, nothing is sent, but the answer stays the same.
    if limiter.charge(
        {
            f"reset-pair:{client}:{address}": RESETS_PER_PAIR,
            f"reset-email:{address}": RESETS_PER_EMAIL,
        }
    ):
        with database.write() as db:
            found = accounts.request_password_reset(db, address)
            if found is not None:
                account, token = found
                emails.queue(
                    db,
                    account.email,
                    emails.password_reset(account, email_link(request, "reset", token)),
                )
    return render(request, "account/reset_requested.html")


def _reset_link_error(request: Request, error: InvalidInputError) -> Response:
    return invalid_link_page(
        request, error, retry_url="/reset", retry_label=_("Ask for a new link")
    )


@router.get("/reset/{token}")
def reset_password_form(
    request: Request, token: str, database: DatabaseDep, user: OptionalUser
) -> Response:
    try:
        with database.read() as db:
            accounts.check_token(db, EmailTokenKind.RESET, token)
    except InvalidInputError as error:
        return _reset_link_error(request, error)
    context = {"token": token, "min_password_length": accounts.MIN_PASSWORD_LENGTH}
    return no_referrer(render(request, "account/reset_form.html", context))


@router.post("/reset/{token}", dependencies=[CsrfProtected])
def reset_password(
    request: Request,
    token: str,
    database: DatabaseDep,
    user: OptionalUser,
    password: Annotated[str, Form()] = "",
    password_confirm: Annotated[str, Form()] = "",
) -> Response:
    context: dict[str, Any] = {"token": token, "min_password_length": accounts.MIN_PASSWORD_LENGTH}
    try:
        if password != password_confirm:
            raise InvalidInputError("password_mismatch")
        with password_slot(), database.write() as db:
            accounts.reset_password(db, token, password)
    except InvalidInputError as error:
        if error.code in LINK_ERRORS:
            return _reset_link_error(request, error)
        context["error"] = error_message(error)
        return no_referrer(render(request, "account/reset_form.html", context, status_code=400))
    response = RedirectResponse("/login", status_code=303)
    delete_cookie(response, request, SESSION_COOKIE)  # every login session has ended
    flash(response, request, "password_reset_done")
    return response
