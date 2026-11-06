"""Persistent email outbox (SPEC §10): an SMTP outage delays emails without losing them.

Emails are queued in the same transaction as the action that triggers them, then sent by
``deliver_pending`` outside any database transaction, so a slow SMTP server never holds the
write lock. Failed sends are retried with exponential backoff, then given up and left visible
to the operator (``agaura email failed``, ``agaura email retry``); an email that cannot even be
built (a malformed address), or whose recipient the server refuses for good (a 5xx answer: no
such mailbox), is given up at once, so it never holds back the ones behind it. Any other failure
(the server unreachable, a login refused, a full mailbox answered 4xx) is retried: it usually
concerns every email, until the operator fixes it. Addresses and bodies (single-use links) are
never logged.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from agaura.db import Database
from agaura.models import OutboxEmail, utcnow

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
FIRST_RETRY_DELAY = timedelta(minutes=1)
MAX_RETRY_DELAY = timedelta(hours=2)
MAX_ERROR_LENGTH = 500


class EmailSender(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class ConsoleSender:
    """No email is sent. In development (``show_body``), the whole message is printed so its
    links can be followed; otherwise only the subject is logged."""

    def __init__(self, *, show_body: bool = False) -> None:
        self.show_body = show_body

    def send(self, message: EmailMessage) -> None:
        if self.show_body:
            # Decoded body: the raw message is quoted-printable (accents escaped, long links
            # wrapped), which would make the links unusable.
            headers = "\n".join(f"{name}: {value}" for name, value in message.items())
            print(
                f"\n----- email (not sent) -----\n{headers}\n\n{message.get_content()}"
                "----------------------------",
                flush=True,
            )
        else:
            logger.info("email not sent (console backend): %s", message["Subject"])


@dataclass(frozen=True, slots=True)
class SmtpSender:
    host: str
    port: int
    username: str
    password: str
    timeout: float = 30.0

    def send(self, message: EmailMessage) -> None:
        context = ssl.create_default_context()
        if self.port == smtplib.SMTP_SSL_PORT:
            with smtplib.SMTP_SSL(
                self.host, self.port, timeout=self.timeout, context=context
            ) as smtp:
                smtp.login(self.username, self.password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                smtp.starttls(context=context)
                smtp.login(self.username, self.password)
                smtp.send_message(message)


def refused_for_good(error: Exception) -> bool:
    """The SMTP server refused every recipient with a permanent (5xx) code."""
    if not isinstance(error, smtplib.SMTPRecipientsRefused) or not error.recipients:
        return False
    return all(500 <= code < 600 for code, _message in error.recipients.values())


def failed_emails(db: Session) -> list[OutboxEmail]:
    """Emails given up, most recent first (kept 90 days, ``agaura.services.retention``)."""
    return list(
        db.scalars(
            select(OutboxEmail)
            .where(OutboxEmail.failed_at.is_not(None))
            .order_by(OutboxEmail.failed_at.desc(), OutboxEmail.id.desc())
        )
    )


def retry(db: Session, email_ids: list[int] | None, *, now: datetime | None = None) -> int:
    """Queue given-up emails again (all of them when ``email_ids`` is None), from their first
    attempt; returns how many."""
    now = now or utcnow()
    emails = [email for email in failed_emails(db) if email_ids is None or email.id in email_ids]
    for email in emails:
        email.failed_at = None
        email.attempts = 0
        email.next_attempt_at = now
    db.flush()
    return len(emails)


def queue_email(
    db: Session, to_address: str, subject: str, body_text: str, *, now: datetime | None = None
) -> OutboxEmail:
    now = now or utcnow()
    email = OutboxEmail(
        to_address=to_address,
        subject=subject,
        body_text=body_text,
        created_at=now,
        next_attempt_at=now,
    )
    db.add(email)
    db.flush()
    return email


def retry_delay(attempts: int) -> timedelta:
    """1 min, 2 min, 4 min… capped at 2 h."""
    return min(FIRST_RETRY_DELAY * 2 ** (attempts - 1), MAX_RETRY_DELAY)


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    sent: int = 0
    retried: int = 0
    given_up: int = 0


def _build_message(from_address: str, to_address: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = from_address
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)
    message.as_bytes()  # errors of serialization show here, not halfway through a send
    return message


def _check_from_address(from_address: str) -> None:
    """A sender address that cannot be a header is a configuration error: it stops the run
    before any email is marked, instead of failing every email of the queue."""
    message = EmailMessage()
    message["From"] = from_address
    message.as_bytes()


def deliver_pending(
    database: Database,
    sender: EmailSender,
    from_address: str,
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> DeliveryReport:
    now = now or utcnow()
    _check_from_address(from_address)
    with database.read() as db:
        due = [
            (email.id, email.to_address, email.subject, email.body_text)
            for email in db.scalars(
                select(OutboxEmail)
                .where(
                    OutboxEmail.sent_at.is_(None),
                    OutboxEmail.failed_at.is_(None),
                    OutboxEmail.next_attempt_at <= now,
                )
                .order_by(OutboxEmail.next_attempt_at, OutboxEmail.id)
                .limit(limit)
            )
        ]

    sent = retried = given_up = 0
    for email_id, to_address, subject, body_text in due:
        error: str | None = None
        permanent = False
        try:
            message = _build_message(from_address, to_address, subject, body_text)
        except Exception as exc:  # whatever is wrong with this row, the next ones still go
            # The exception text may quote the faulty header: only its type is kept.
            error = f"message cannot be built: {type(exc).__name__}"
            permanent = True
            logger.error("email %s cannot be built, given up: %s", email_id, type(exc).__name__)
        else:
            try:
                sender.send(message)
            except (OSError, smtplib.SMTPException) as exc:
                error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
                permanent = refused_for_good(exc)
                logger.warning("email %s not sent: %s", email_id, type(exc).__name__)

        with database.write() as db:
            email = db.get(OutboxEmail, email_id)
            if email is None:  # deleted meanwhile (account deletion)
                continue
            email.attempts += 1
            if error is None:
                email.sent_at = now
                sent += 1
                continue
            email.last_error = error
            if permanent or email.attempts >= MAX_ATTEMPTS:
                email.failed_at = now
                given_up += 1
            else:
                email.next_attempt_at = now + retry_delay(email.attempts)
                retried += 1
    return DeliveryReport(sent=sent, retried=retried, given_up=given_up)
