"""Invitations: the person invited by the super-admin chooses a password through the link
received by email, which also confirms the address, and is logged in."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from agaura.db import Database
from agaura.i18n import _
from agaura.models import EmailTokenKind
from agaura.security import password_slot
from agaura.services import accounts, auth
from agaura.services.errors import InvalidInputError
from agaura.web.account.common import (
    INVITATION_ATTEMPTS_PER_IP,
    LINK_ERRORS,
    invalid_link_page,
    no_referrer,
)
from agaura.web.deps import SESSION_COOKIE, CsrfProtected, DatabaseDep, LimiterDep
from agaura.web.limits import limit_address
from agaura.web.messages import error_message
from agaura.web.public import COOKIE_MAX_AGE
from agaura.web.security import read_cookie, set_cookie
from agaura.web.templating import flash, render

router = APIRouter()


def _link_error(request: Request, error: InvalidInputError) -> Response:
    return invalid_link_page(
        request,
        error,
        retry_url="/login",
        retry_label=_("Go to the login page"),
        hint=_(
            "If you have not chosen a password yet, ask the person who created your account to "
            "send a new invitation."
        ),
    )


def _form(
    request: Request,
    database: Database,
    token: str,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    """The password form, or the invalid link page when the link no longer works."""
    try:
        with database.read() as db:
            email = accounts.check_token(db, EmailTokenKind.INVITE, token).email
    except InvalidInputError as link_error:
        return _link_error(request, link_error)
    context: dict[str, Any] = {
        "token": token,
        "email": email,
        "min_password_length": accounts.MIN_PASSWORD_LENGTH,
    }
    if error:
        context["error"] = error_message(error)
    return no_referrer(render(request, "account/invitation.html", context, status_code=status_code))


@router.get("/invite/{token}")
def invitation_form(request: Request, token: str, database: DatabaseDep) -> Response:
    return _form(request, database, token)


@router.post("/invite/{token}", dependencies=[CsrfProtected])
def accept_invitation(
    request: Request,
    token: str,
    database: DatabaseDep,
    limiter: LimiterDep,
    password: Annotated[str, Form()] = "",
    password_confirm: Annotated[str, Form()] = "",
) -> Response:
    ip_key = f"invite-ip:{limit_address(request)}"
    if not limiter.charge({ip_key: INVITATION_ATTEMPTS_PER_IP}):
        return _form(request, database, token, error="too_many_attempts", status_code=429)
    try:
        if password != password_confirm:
            raise InvalidInputError("password_mismatch")
        # The hashing turn is taken before the write lock, which votes need (agaura.security).
        with password_slot(), database.write() as db:
            account = accounts.accept_invitation(db, token, password)
            # Whoever was logged in on this browser before is logged out.
            auth.end_session(db, read_cookie(request, SESSION_COOKIE))
            session_token = auth.start_session(
                db, account, user_agent=request.headers.get("user-agent")
            )
    except InvalidInputError as error:
        if error.code in LINK_ERRORS:
            return _link_error(request, error)
        return _form(request, database, token, error=error.code, status_code=400)
    response = RedirectResponse("/dashboard", status_code=303)
    set_cookie(response, request, SESSION_COOKIE, session_token, max_age=COOKIE_MAX_AGE)
    flash(response, request, "account_invitation_accepted")
    return no_referrer(response)
