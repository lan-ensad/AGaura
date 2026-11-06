import datetime as dt
import logging
import smtplib
from email.message import EmailMessage
from typing import Any

import pytest
from sqlalchemy import delete

from agaura.db import Database
from agaura.models import OutboxEmail, utcnow
from agaura.services import outbox

FROM = "AGaura <no-reply@agaura.org>"


class FakeSender:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        if self.failures:
            self.failures -= 1
            raise smtplib.SMTPServerDisconnected("connection lost")
        self.sent.append(message)


def queue(database: Database, now: dt.datetime) -> int:
    with database.write() as db:
        return outbox.queue_email(db, "a@example.org", "Bienvenue", "Lien\u00a0: …", now=now).id


def email(database: Database, email_id: int) -> OutboxEmail:
    with database.read() as db:
        row = db.get(OutboxEmail, email_id)
        assert row is not None
        return row


def test_delivery(database: Database) -> None:
    now = utcnow()
    email_id = queue(database, now)
    sender = FakeSender()
    report = outbox.deliver_pending(database, sender, FROM, now=now)
    assert report == outbox.DeliveryReport(sent=1)
    assert [m["To"] for m in sender.sent] == ["a@example.org"]
    assert sender.sent[0]["From"] == FROM
    assert email(database, email_id).sent_at == now
    assert outbox.deliver_pending(database, sender, FROM, now=now) == outbox.DeliveryReport()


def test_retries_with_backoff_then_gives_up(database: Database) -> None:
    now = utcnow()
    email_id = queue(database, now)
    sender = FakeSender(failures=outbox.MAX_ATTEMPTS)
    report = outbox.deliver_pending(database, sender, FROM, now=now)
    assert report == outbox.DeliveryReport(retried=1)
    row = email(database, email_id)
    assert row.next_attempt_at == now + dt.timedelta(minutes=1)
    assert row.last_error is not None and "SMTPServerDisconnected" in row.last_error
    # Not due yet: nothing happens.
    assert outbox.deliver_pending(database, sender, FROM, now=now) == outbox.DeliveryReport()
    for _ in range(outbox.MAX_ATTEMPTS - 1):
        now = email(database, email_id).next_attempt_at
        report = outbox.deliver_pending(database, sender, FROM, now=now)
    assert report == outbox.DeliveryReport(given_up=1)
    assert email(database, email_id).failed_at == now


@pytest.mark.parametrize(
    ("to_address", "subject"),
    [
        ("x\n@evil.example", "Bienvenue"),
        ("a@example.org", "Bienvenue\r\nBcc: spy@evil.example"),
    ],
)
def test_unbuildable_email_is_given_up_without_holding_the_queue(
    database: Database, caplog: pytest.LogCaptureFixture, to_address: str, subject: str
) -> None:
    now = utcnow()
    with database.write() as db:  # older than the valid email: first in the queue
        bad = OutboxEmail(
            to_address=to_address,
            subject=subject,
            body_text="Lien\u00a0: SECRET-TOKEN",
            created_at=now,
            next_attempt_at=now - dt.timedelta(minutes=1),
        )
        db.add(bad)
        db.flush()
        bad_id = bad.id
    good_id = queue(database, now)
    sender = FakeSender()
    with caplog.at_level(logging.INFO):
        report = outbox.deliver_pending(database, sender, FROM, now=now)
    assert report == outbox.DeliveryReport(sent=1, given_up=1)
    assert [m["To"] for m in sender.sent] == ["a@example.org"]
    assert email(database, good_id).sent_at == now
    row = email(database, bad_id)
    assert (row.failed_at, row.sent_at, row.attempts) == (now, None, 1)
    assert row.last_error == "message cannot be built: ValueError"
    assert f"email {bad_id} cannot be built" in caplog.text
    assert "evil.example" not in caplog.text and "SECRET-TOKEN" not in caplog.text
    # Given up for good: later runs leave it alone.
    later = now + outbox.MAX_RETRY_DELAY
    assert outbox.deliver_pending(database, sender, FROM, now=later) == outbox.DeliveryReport()


def test_wrong_sender_address_stops_the_run_and_marks_nothing(database: Database) -> None:
    now = utcnow()
    email_id = queue(database, now)
    with pytest.raises(ValueError, match="linefeed"):
        outbox.deliver_pending(database, FakeSender(), "AGaura\n<no-reply@agaura.org>", now=now)
    row = email(database, email_id)
    assert (row.attempts, row.failed_at, row.sent_at, row.last_error) == (0, None, None, None)


def test_retry_delay_is_capped() -> None:
    assert outbox.retry_delay(1) == dt.timedelta(minutes=1)
    assert outbox.retry_delay(3) == dt.timedelta(minutes=4)
    assert outbox.retry_delay(20) == outbox.MAX_RETRY_DELAY


def test_email_deleted_while_sending(database: Database) -> None:
    now = utcnow()
    queue(database, now)

    class DeletingSender:
        def send(self, message: EmailMessage) -> None:
            with database.write() as db:
                db.execute(delete(OutboxEmail))

    assert outbox.deliver_pending(database, DeletingSender(), FROM, now=now) == (
        outbox.DeliveryReport()
    )


def test_console_sender_never_logs_the_body(caplog: pytest.LogCaptureFixture) -> None:
    message = EmailMessage()
    message["Subject"] = "Réinitialisation"
    message["To"] = "a@example.org"
    message.set_content("https://agaura.org/reset/SECRET-TOKEN")
    with caplog.at_level(logging.INFO):
        outbox.ConsoleSender().send(message)
    assert "Réinitialisation" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text and "a@example.org" not in caplog.text


@pytest.mark.parametrize(("port", "smtp_class"), [(465, "SMTP_SSL"), (587, "SMTP")])
def test_smtp_sender(monkeypatch: pytest.MonkeyPatch, port: int, smtp_class: str) -> None:
    calls: list[str] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, **kwargs: Any) -> None:
            calls.append(f"connect {host}:{port}")

        def __enter__(self) -> "FakeSMTP":
            return self

        def __exit__(self, *args: object) -> None:
            calls.append("quit")

        def starttls(self, **kwargs: Any) -> None:
            calls.append("starttls")

        def login(self, username: str, password: str) -> None:
            calls.append(f"login {username}")

        def send_message(self, message: EmailMessage) -> None:
            calls.append("send")

    monkeypatch.setattr(smtplib, smtp_class, FakeSMTP)
    sender = outbox.SmtpSender("mail.example.org", port, "no-reply@agaura.org", "secret")
    sender.send(EmailMessage())
    expected = [f"connect mail.example.org:{port}", "login no-reply@agaura.org", "send", "quit"]
    if port == 587:
        expected.insert(1, "starttls")
    assert calls == expected


def test_console_sender_prints_the_whole_email_in_development(
    capsys: pytest.CaptureFixture[str],
) -> None:
    message = EmailMessage()
    message["Subject"] = "Vérification"
    link = "http://localhost:8000/account/email/" + "T" * 60  # longer than a 76-character line
    message.set_content(f"Confirmez votre adresse e-mail\u00a0:\n{link}\n")
    outbox.ConsoleSender(show_body=True).send(message)
    printed = capsys.readouterr().out
    assert link in printed and "Confirmez votre adresse e-mail" in printed


class RefusingSender:
    def __init__(self, code: int) -> None:
        self.code = code

    def send(self, message: EmailMessage) -> None:
        raise smtplib.SMTPRecipientsRefused({str(message["To"]): (self.code, b"5.1.1 unknown")})


@pytest.mark.parametrize(("code", "given_up"), [(550, 1), (452, 0)])
def test_a_recipient_refused_for_good_is_given_up_at_once(
    database: Database, code: int, given_up: int
) -> None:
    """No such mailbox (5xx): retrying for hours changes nothing. A full mailbox or a busy
    server (4xx) may accept later."""
    now = utcnow()
    email_id = queue(database, now)
    report = outbox.deliver_pending(database, RefusingSender(code), FROM, now=now)
    assert report == outbox.DeliveryReport(given_up=given_up, retried=1 - given_up)
    row = email(database, email_id)
    assert (row.failed_at == now) is bool(given_up)
    assert row.last_error is not None and "SMTPRecipientsRefused" in row.last_error


def test_refused_for_good() -> None:
    assert not outbox.refused_for_good(smtplib.SMTPRecipientsRefused({}))
    assert not outbox.refused_for_good(smtplib.SMTPAuthenticationError(535, b"bad login"))
    mixed = {"a@x.org": (550, b""), "b@x.org": (451, b"")}
    assert not outbox.refused_for_good(smtplib.SMTPRecipientsRefused(mixed))


def test_retry_given_up_emails(database: Database) -> None:
    now = utcnow()
    first, second, pending = queue(database, now), queue(database, now), queue(database, now)
    with database.write() as db:
        for email_id in (first, second):
            row = db.get(OutboxEmail, email_id)
            assert row is not None
            row.failed_at, row.attempts = now, outbox.MAX_ATTEMPTS
    with database.read() as db:
        assert [row.id for row in outbox.failed_emails(db)] == [second, first]
    later = now + dt.timedelta(hours=1)
    with database.write() as db:
        assert outbox.retry(db, [first, pending], now=later) == 1
    row = email(database, first)
    assert (row.failed_at, row.attempts, row.next_attempt_at) == (None, 0, later)
    with database.write() as db:
        assert outbox.retry(db, None, now=later) == 1
    assert email(database, second).failed_at is None
