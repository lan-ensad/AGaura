"""Account settings: name, email, password, login sessions, deletion and the account log
(FR-03, FR-05, FR-06, FR-84)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from agaura.db import Database
from agaura.i18n import N_, _
from agaura.models import AuditEntry, User
from agaura.security import password_slot
from agaura.services import accounts, auth, emails
from agaura.services.audit import Actor
from agaura.services.errors import InvalidInputError, ServiceError
from agaura.web.account.common import (
    EMAIL_CHANGES_PER_ACCOUNT,
    VERIFICATION_RESENDS_PER_ACCOUNT,
    email_link,
    invalid_link_page,
    password_attempt,
    password_limits,
)
from agaura.web.deps import (
    SESSION_COOKIE,
    CsrfProtected,
    DatabaseDep,
    LimiterDep,
    LoggedInUser,
    OptionalUser,
)
from agaura.web.messages import error_message
from agaura.web.public import COOKIE_MAX_AGE
from agaura.web.security import delete_cookie, set_cookie
from agaura.web.templating import flash, render

router = APIRouter()

ACTION_LABELS = {
    "account.created": N_("Account created"),
    "account.login": N_("Logged in"),
    "account.login_failed": N_("Failed login"),
    "account.logged_out_everywhere": N_("Logged out on every device"),
    "account.email_verified": N_("Email address confirmed"),
    "account.password_reset_requested": N_("Password reset requested"),
    "account.password_reset": N_("Password reset"),
    "account.password_changed": N_("Password changed"),
    "account.email_change_requested": N_("Email change requested"),
    "account.email_changed": N_("Email address changed"),
    "account.display_name_changed": N_("Name changed"),
    "account.disabled": N_("Account disabled"),
    "account.enabled": N_("Account enabled again"),
    "account.quotas_changed": N_("Account limits changed"),
    "account.invited": N_("Account created, invitation sent"),
    "account.invitation_resent": N_("Invitation sent again"),
    "account.invitation_accepted": N_("Invitation accepted, password chosen"),
    "account.demo_started": N_("Demonstration opened"),
}
LOGIN_FAILURE_REASONS = {
    "password": N_("wrong password"),
    "disabled": N_("account disabled"),
    "pending": N_("invitation not accepted yet"),
}


@dataclass(frozen=True, slots=True)
class LogLine:
    at: datetime
    label: str
    detail: str | None


def _log_line(entry: AuditEntry) -> LogLine:
    label = ACTION_LABELS.get(entry.action)
    detail = None
    if entry.action == "account.login_failed":
        reason = LOGIN_FAILURE_REASONS.get(str(entry.details.get("reason")))
        detail = _(reason) if reason else None
    elif entry.actor == Actor.OPERATOR:
        detail = _("by the platform operator")
    elif entry.actor == accounts.SUPERADMIN_ACTOR:
        detail = _("by the accounts administrator")
    return LogLine(at=entry.at, label=_(label) if label else entry.action, detail=detail)


def _account_page(
    request: Request,
    database: Database,
    user: User,
    *,
    error: ServiceError | str | None = None,
    status_code: int = 200,
    form: dict[str, str] | None = None,
) -> Response:
    with database.read() as db:
        log = [_log_line(entry) for entry in accounts.account_log(db, user)]
        pending_email = accounts.pending_email_change(db, user)
    context: dict[str, Any] = {
        "log": log,
        "pending_email": pending_email,
        "min_password_length": accounts.MIN_PASSWORD_LENGTH,
        "form": {"display_name": user.display_name or "", "new_email": "", **(form or {})},
    }
    if error is not None:
        context["error"] = error_message(error)
    return render(request, "account/settings.html", context, status_code=status_code)


def _back_to_account(request: Request, notice: str) -> Response:
    response = RedirectResponse("/account", status_code=303)
    flash(response, request, notice)
    return response


@router.get("/account")
def account(request: Request, user: LoggedInUser, database: DatabaseDep) -> Response:
    return _account_page(request, database, user)


@router.post("/account/name", dependencies=[CsrfProtected])
def change_name(
    request: Request,
    user: LoggedInUser,
    database: DatabaseDep,
    display_name: Annotated[str, Form()] = "",
) -> Response:
    try:
        with database.write() as db:
            accounts.update_display_name(db, user, display_name)
    except InvalidInputError as error:
        return _account_page(
            request,
            database,
            user,
            error=error,
            status_code=400,
            form={"display_name": display_name},
        )
    return _back_to_account(request, "name_updated")


@router.post("/account/password", dependencies=[CsrfProtected])
def change_password(
    request: Request,
    user: LoggedInUser,
    database: DatabaseDep,
    limiter: LimiterDep,
    current_password: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
    new_password_confirm: Annotated[str, Form()] = "",
) -> Response:
    if not limiter.charge(password_limits(user)):
        return _account_page(request, database, user, error="too_many_attempts", status_code=429)
    try:
        with password_attempt(limiter, user):
            if new_password != new_password_confirm:
                raise InvalidInputError("password_mismatch")
            with password_slot(), database.write() as db:
                token = accounts.change_password(
                    db,
                    user,
                    current_password,
                    new_password,
                    user_agent=request.headers.get("user-agent"),
                )
    except InvalidInputError as error:
        return _account_page(request, database, user, error=error, status_code=400)
    response = _back_to_account(request, "account_password_changed")
    # A new login session for this device: the previous cookie no longer works anywhere.
    set_cookie(response, request, SESSION_COOKIE, token, max_age=COOKIE_MAX_AGE)
    return response


@router.post("/account/email", dependencies=[CsrfProtected])
def request_email_change(
    request: Request,
    user: LoggedInUser,
    database: DatabaseDep,
    limiter: LimiterDep,
    new_email: Annotated[str, Form()] = "",
    email_password: Annotated[str, Form()] = "",
) -> Response:
    form = {"new_email": new_email}
    # Every request counts, whatever its outcome: the form must not become a way to test
    # addresses (the answer is the same for a taken one, but the email sent is not).
    limits = {f"email-change:{user.id}": EMAIL_CHANGES_PER_ACCOUNT, **password_limits(user)}
    if not limiter.charge(limits):
        return _account_page(
            request, database, user, error="too_many_attempts", status_code=429, form=form
        )
    try:
        with password_attempt(limiter, user), password_slot(), database.write() as db:
            # The service queues the notice to the current address, and the email to a new
            # address that already has an account; the link goes to a free new address.
            address, token = accounts.request_email_change(db, user, email_password, new_email)
            if token is not None:
                emails.queue(
                    db,
                    address,
                    emails.email_change(user, email_link(request, "account/email", token)),
                )
    except InvalidInputError as error:
        return _account_page(request, database, user, error=error, status_code=400, form=form)
    return _back_to_account(request, "email_change_sent")


@router.get("/account/email/{token}")
def confirm_email_change(
    request: Request, token: str, database: DatabaseDep, user: OptionalUser
) -> Response:
    try:
        with database.write() as db:
            account, previous = accounts.change_email(db, token)
            emails.queue(db, previous, emails.email_changed(account))
    except InvalidInputError as error:
        return invalid_link_page(
            request, error, retry_url="/account", retry_label=_("Go to your account")
        )
    same_account = user is not None and user.id == account.id
    response = RedirectResponse(
        "/account" if same_account else "/login?next=/account", status_code=303
    )
    flash(response, request, "email_changed")
    return response


@router.post("/account/verify/resend", dependencies=[CsrfProtected])
def resend_verification(
    request: Request, user: LoggedInUser, database: DatabaseDep, limiter: LimiterDep
) -> Response:
    key = f"verify-resend:{user.id}"
    if not limiter.charge({key: VERIFICATION_RESENDS_PER_ACCOUNT}):
        return _account_page(request, database, user, error="too_many_attempts", status_code=429)
    try:
        with database.write() as db:
            found = accounts.request_verification(db, user)
            if found is not None:
                account, token = found
                emails.queue(
                    db,
                    account.email,
                    emails.verification(account, email_link(request, "verify", token)),
                )
    except BaseException:
        limiter.refund(key)
        raise
    if found is None:
        limiter.refund(key)  # nothing sent
        return _back_to_account(request, "email_verified")
    return _back_to_account(request, "verification_sent")


@router.post("/account/logout-everywhere", dependencies=[CsrfProtected])
def logout_everywhere(request: Request, user: LoggedInUser, database: DatabaseDep) -> Response:
    with database.write() as db:
        auth.end_all_sessions(db, user)
    response = RedirectResponse("/login", status_code=303)
    delete_cookie(response, request, SESSION_COOKIE)
    flash(response, request, "logged_out_everywhere")
    return response


@router.post("/account/delete", dependencies=[CsrfProtected])
def delete_account(
    request: Request,
    user: LoggedInUser,
    database: DatabaseDep,
    limiter: LimiterDep,
    delete_password: Annotated[str, Form()] = "",
) -> Response:
    if not limiter.charge(password_limits(user)):
        return _account_page(request, database, user, error="too_many_attempts", status_code=429)
    try:
        with password_attempt(limiter, user), password_slot(), database.write() as db:
            accounts.delete_account(db, user, delete_password)
    except ServiceError as error:
        return _account_page(request, database, user, error=error, status_code=400)
    response = RedirectResponse("/", status_code=303)
    delete_cookie(response, request, SESSION_COOKIE)
    flash(response, request, "account_deleted")
    return response
