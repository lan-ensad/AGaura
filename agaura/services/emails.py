"""Texts of the account emails (plain text, French through gettext) and their queuing.

Emails go through the outbox (``outbox.queue_email``) in the transaction of the action that
triggers them. Links are built by the web layer from the public base address; bodies are never
logged, since they carry single-use links.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from babel.dates import format_date
from sqlalchemy.orm import Session

from agaura.i18n import DEFAULT_LOCALE, _
from agaura.models import User
from agaura.services import outbox

DISPLAY_TIMEZONE = ZoneInfo("Europe/Paris")  # as the web pages (agaura/web/templating.py)


@dataclass(frozen=True, slots=True)
class Email:
    subject: str
    body: str


def _greeting(name: str | None) -> str:
    return _("Hello %(name)s,") % {"name": name} if name else _("Hello,")


def _body(*paragraphs: str) -> str:
    signature = "-- \n" + _("AGaura · Voting for in-person general assemblies")
    return "\n\n".join([*paragraphs, signature]) + "\n"


def verification(user: User, link: str) -> Email:
    return Email(
        subject=_("Confirm your email address"),
        body=_body(
            _greeting(user.display_name),
            _("Welcome to AGaura. To confirm your email address, open this link within 24 hours:"),
            link,
            _("Your address must be confirmed before you can create a session."),
            _("If you did not create this account, ignore this email."),
        ),
    )


def password_reset(user: User, link: str) -> Email:
    return Email(
        subject=_("Reset your password"),
        body=_body(
            _greeting(user.display_name),
            _(
                "A password reset was requested for your AGaura account. To choose a new "
                "password, open this link within one hour:"
            ),
            link,
            _("If you did not ask for it, ignore this email: your password stays the same."),
        ),
    )


def email_change(user: User, link: str) -> Email:
    return Email(
        subject=_("Confirm your new email address"),
        body=_body(
            _greeting(user.display_name),
            _(
                "This address was entered as the new email address of an AGaura account. To "
                "confirm the change, open this link within 24 hours:"
            ),
            link,
            _("If you did not ask for it, ignore this email: nothing will change."),
        ),
    )


def email_change_requested(user: User, new_email: str) -> Email:
    """Notice sent to the current address when a change is requested, while it can still be
    cancelled."""
    return Email(
        subject=_("Change of your email address requested"),
        body=_body(
            _greeting(user.display_name),
            _(
                "A change of the email address of your AGaura account was requested: the new "
                "address would be %(email)s. The change applies only once the link sent to "
                "that address is opened, within 24 hours."
            )
            % {"email": new_email},
            _(
                "If you did not ask for it, someone else may know your password: change it at "
                'once, from your account or with "Forgot your password?" on the login page. '
                'This cancels the change, and so does "Log out everywhere" on your account page.'
            ),
        ),
    )


def address_in_use(owner: User) -> Email:
    """Sent, instead of the confirmation link, to an address entered as the new address of
    another account while it already has one (the form answers the same either way)."""
    return Email(
        subject=_("Your email address was entered for another AGaura account"),
        body=_body(
            _greeting(owner.display_name),
            _(
                "This address was entered as the new email address of another AGaura account. "
                "It cannot be used for it: it is already the address of your AGaura account, "
                "which stays as it is."
            ),
            _(
                "If you made this request, choose another address for the other account. If "
                "not, ignore this email: nothing will change."
            ),
        ),
    )


def email_changed(user: User) -> Email:
    """Notice sent to the previous address once a change is applied."""
    return Email(
        subject=_("Your email address was changed"),
        body=_body(
            _greeting(user.display_name),
            _("The email address of your AGaura account is now %(email)s.") % {"email": user.email},
            _(
                "If you did not make this change, someone else may have access to your "
                "account: contact the AGaura operator."
            ),
        ),
    )


def invitation(user: User, link: str) -> Email:
    """Sent by the super-admin when creating an account (and again on request)."""
    return Email(
        subject=_("Your AGaura account"),
        body=_body(
            _greeting(user.display_name),
            _(
                "An AGaura account has been created for you with this email address. AGaura "
                "runs the votes of in-person general assemblies: with it, you can create "
                "sessions and push their polls to the members' phones."
            ),
            _("To choose your password and start using the account, open this link within 7 days:"),
            link,
            _(
                "The link works once. If it has expired, ask the person who created the account "
                "for a new one."
            ),
            _("If you did not expect this email, ignore it: the account stays inactive."),
        ),
    )


def inactivity_warning(user: User, link: str, deleted_on: datetime) -> Email:
    """Sent 30 days before an account inactive for 3 years is deleted (D12)."""
    day = format_date(deleted_on.astimezone(DISPLAY_TIMEZONE), format="long", locale=DEFAULT_LOCALE)
    return Email(
        subject=_("Your AGaura account will be deleted"),
        body=_body(
            _greeting(user.display_name),
            _(
                "Your AGaura account (%(email)s) has not been used for three years. AGaura keeps "
                "no account longer: it will be deleted with its sessions on %(date)s."
            )
            % {"email": user.email, "date": day},
            _("To keep it, log in before that date:"),
            link,
            _(
                "If you no longer need it, there is nothing to do. To keep what your past "
                "sessions recorded, download their minutes and exports before that date."
            ),
        ),
    )


def queue(db: Session, to_address: str, email: Email, *, now: datetime | None = None) -> None:
    outbox.queue_email(db, to_address, email.subject, email.body, now=now)
