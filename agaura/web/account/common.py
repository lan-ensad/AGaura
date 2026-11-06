"""Shared pieces of the accounts area: messages, rate limits, email links, the invalid link
page."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import Request
from starlette.responses import Response

from agaura.i18n import N_
from agaura.models import User
from agaura.services.errors import ServiceError
from agaura.web.deps import public_base_url
from agaura.web.messages import ERRORS, NOTICES, error_message
from agaura.web.security import RateLimiter
from agaura.web.templating import render

ERRORS.update(
    {
        "pow_failed": N_(
            "The anti-robot check did not succeed. Wait until it is complete, then try again."
        ),
        "password_mismatch": N_("The two passwords do not match."),
        "email_unavailable": N_(
            "This email address cannot be used. If it is yours, log in or reset your password."
        ),
        "wrong_password": N_("Incorrect password."),
        "same_email": N_("This is already your email address."),
        "invalid_link": N_("This link is invalid or has already been used."),
        "expired_link": N_("This link has expired."),
        "account_in_session": N_(
            "One of your sessions is in session. Close it before deleting your account."
        ),
    }
)

NOTICES.update(
    {
        "signed_up": N_(
            "Your account is created. Confirm your email address with the link just sent to you."
        ),
        "email_verified": N_("Your email address is confirmed."),
        "verification_sent": N_("A new confirmation link has been sent to your email address."),
        "password_reset_done": N_("Your password has been changed. Log in with the new one."),
        "account_password_changed": N_(
            "Your password has been changed. Your other devices have been logged out."
        ),
        "name_updated": N_("Your name has been saved."),
        "email_change_sent": N_(
            "A confirmation link has been sent to the new address. The change applies once "
            "it is opened."
        ),
        "email_changed": N_("Your email address has been changed."),
        "logged_out_everywhere": N_("You are logged out on every device."),
        "account_deleted": N_("Your account and all its sessions have been deleted."),
        "account_invitation_accepted": N_(
            "Welcome to AGaura. Your password is saved and your email address is confirmed."
        ),
    }
)

# (limit, window in seconds). Per-IP limits stay generous: mobile carriers share IPs (CGNAT).
SIGNUPS_PER_IP = (10, 3600.0)
RESETS_PER_IP = (30, 3600.0)
# Reset emails to one address: 3 an hour asked from one client address, 10 from all of them
# together, which bounds the flood a mailbox can receive. Past either, nothing is sent and the
# answer stays the same. Failures elsewhere never use up the owner's own 3, and while the cap of
# 10 holds, the latest link sent is still valid (it lasts 1 h, as long as the window), so the
# owner can always use it.
RESETS_PER_PAIR = (3, 3600.0)
RESETS_PER_EMAIL = (10, 3600.0)
INVITATION_ATTEMPTS_PER_IP = (30, 3600.0)  # each attempt hashes a password
VERIFICATION_RESENDS_PER_ACCOUNT = (3, 3600.0)
EMAIL_CHANGES_PER_ACCOUNT = (5, 3600.0)  # every request, whatever its outcome
PASSWORD_FAILURES_PER_ACCOUNT = (10, 900.0)

LINK_ERRORS = frozenset({"invalid_link", "expired_link"})


def _password_key(user: User) -> str:
    return f"account-password:{user.id}"


def password_limits(user: User) -> dict[str, tuple[int, float]]:
    """For ``RateLimiter.charge``, before a form that asks for the account's own password: the
    attempt counts before the password is checked, so that concurrent requests see each other
    (``password_attempt``). One counter per account, whatever the form."""
    return {_password_key(user): PASSWORD_FAILURES_PER_ACCOUNT}


@contextmanager
def password_attempt(limiter: RateLimiter, user: User) -> Iterator[None]:
    """Around the step that checks the password, once ``password_limits`` is charged: the
    attempt stays counted only if the password was wrong."""
    wrong = False
    try:
        yield
    except ServiceError as error:
        wrong = error.code == "wrong_password"
        raise
    finally:
        if not wrong:
            limiter.refund(_password_key(user))


def email_link(request: Request, path: str, token: str) -> str:
    """Absolute address of an email link, e.g. ``email_link(request, "verify", token)``."""
    return f"{public_base_url(request)}/{path}/{token}"


def no_referrer(response: Response) -> Response:
    """Pages whose address holds a token must not pass it on in the Referer header."""
    response.headers["referrer-policy"] = "no-referrer"
    return response


def invalid_link_page(
    request: Request,
    error: ServiceError,
    *,
    retry_url: str,
    retry_label: str,
    hint: str | None = None,
) -> Response:
    context: dict[str, Any] = {
        "message": error_message(error),
        "expired": error.code == "expired_link",
        "retry_url": retry_url,
        "retry_label": retry_label,
        "hint": hint,
    }
    return no_referrer(render(request, "account/link_invalid.html", context, status_code=400))
