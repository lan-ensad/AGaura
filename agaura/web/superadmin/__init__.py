"""Super-admin area (``/superadmin``): invite and delete accounts, nothing else.

The super-admin is created on the command line only (``agaura user create EMAIL --superadmin``).
Its pages show accounts (address, name, dates, state) and never anything about sessions; the
console and the dashboard refuse it (``deps.current_user``). Account holders get the 404 page
here.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from agaura.db import Database
from agaura.i18n import N_
from agaura.models import User
from agaura.security import password_slot
from agaura.services import accounts, emails
from agaura.services.errors import NotFoundError, ServiceError
from agaura.web.account.common import email_link, password_attempt, password_limits
from agaura.web.deps import CsrfProtected, DatabaseDep, LimiterDep, RowId, SuperAdmin
from agaura.web.messages import ERRORS, NOTICES, error_message
from agaura.web.security import RateLimiter
from agaura.web.templating import flash, flash_error, render

router = APIRouter()

ERRORS.update(
    {
        "superadmin_protected": N_(
            "A super-admin account cannot be deleted here: the platform operator manages it on "
            "the server."
        ),
        "superadmin_not_pending": N_(
            "This account is not waiting for an invitation: its password is already chosen, or "
            "it is disabled."
        ),
        "superadmin_confirm_mismatch": N_(
            "The address typed does not match the account. Nothing was deleted."
        ),
        "superadmin_account_in_session": N_(
            "A session of this account is in progress. The account can be deleted once that "
            "session is closed."
        ),
        "superadmin_no_sessions": N_("The super-admin account cannot create sessions."),
    }
)
NOTICES.update(
    {
        "superadmin_invited": N_(
            "Account created. The invitation has been sent; its link is valid for 7 days."
        ),
        "superadmin_invitation_resent": N_(
            "A new invitation has been sent. The previous link no longer works."
        ),
        "superadmin_deleted": N_("Account deleted, with all its sessions."),
    }
)

# Invitations and new links sent by one super-admin; generous, but caps a stolen session.
INVITATIONS_PER_SUPERADMIN = (50, 3600.0)

# Service error codes shown with a wording of this area.
ERROR_CODES = {"account_in_session": "superadmin_account_in_session"}


def _accounts_page(
    request: Request,
    database: Database,
    admin: User,
    *,
    form: dict[str, str] | None = None,
    error: ServiceError | str | None = None,
    status_code: int = 200,
) -> Response:
    with database.read() as db:
        rows = accounts.list_accounts(db, admin)
    context: dict[str, Any] = {
        "accounts": rows,
        "form": {"email": "", "display_name": "", **(form or {})},
        "states": accounts.AccountState,
    }
    if error is not None:
        context["error"] = error_message(error)
    return render(request, "superadmin/accounts.html", context, status_code=status_code)


def _back(request: Request, *, notice: str | None = None, error: str | None = None) -> Response:
    response = RedirectResponse("/superadmin", status_code=303)
    if error is not None:
        flash_error(response, request, ERROR_CODES.get(error, error))
    elif notice is not None:
        flash(response, request, notice)
    return response


def _invitations_key(admin: User) -> str:
    return f"superadmin-invitations:{admin.id}"


def _too_many_invitations(limiter: RateLimiter, admin: User) -> bool:
    return limiter.exceeded(_invitations_key(admin), *INVITATIONS_PER_SUPERADMIN)


@router.get("/superadmin", response_class=HTMLResponse)
def accounts_page(request: Request, admin: SuperAdmin, database: DatabaseDep) -> Response:
    return _accounts_page(request, database, admin)


@router.post("/superadmin/invite", dependencies=[CsrfProtected])
def invite(
    request: Request,
    admin: SuperAdmin,
    database: DatabaseDep,
    limiter: LimiterDep,
    email: Annotated[str, Form()] = "",
    display_name: Annotated[str, Form()] = "",
) -> Response:
    form = {"email": email, "display_name": display_name}
    if _too_many_invitations(limiter, admin):
        return _accounts_page(
            request, database, admin, form=form, error="too_many_attempts", status_code=429
        )
    try:
        with database.write() as db:
            account, token = accounts.invite(db, admin, email, display_name=display_name)
            emails.queue(
                db, account.email, emails.invitation(account, email_link(request, "invite", token))
            )
    except ServiceError as error:
        return _accounts_page(request, database, admin, form=form, error=error, status_code=400)
    limiter.record(_invitations_key(admin))
    return _back(request, notice="superadmin_invited")


@router.post("/superadmin/accounts/{account_id}/resend", dependencies=[CsrfProtected])
def resend_invitation(
    request: Request,
    account_id: RowId,
    admin: SuperAdmin,
    database: DatabaseDep,
    limiter: LimiterDep,
) -> Response:
    if _too_many_invitations(limiter, admin):
        return _back(request, error="too_many_attempts")
    try:
        with database.write() as db:
            account, token = accounts.resend_invitation(db, admin, account_id)
            emails.queue(
                db, account.email, emails.invitation(account, email_link(request, "invite", token))
            )
    except NotFoundError:
        raise
    except ServiceError as error:
        return _back(request, error=error.code)
    limiter.record(_invitations_key(admin))
    return _back(request, notice="superadmin_invitation_resent")


@router.post("/superadmin/accounts/{account_id}/delete", dependencies=[CsrfProtected])
def delete_account(
    request: Request,
    account_id: RowId,
    admin: SuperAdmin,
    database: DatabaseDep,
    limiter: LimiterDep,
    confirm_email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    """Delete an account once its address and the super-admin's own password are typed; wrong
    passwords count against the super-admin account, as in its settings."""
    if not limiter.charge(password_limits(admin)):
        return _accounts_page(request, database, admin, error="too_many_attempts", status_code=429)
    try:
        with password_attempt(limiter, admin), password_slot(), database.write() as db:
            accounts.delete_account_by_superadmin(
                db, admin, account_id, confirm_email, password=password
            )
    except NotFoundError:
        raise
    except ServiceError as error:
        return _back(request, error=error.code)
    return _back(request, notice="superadmin_deleted")
