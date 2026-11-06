"""Retention (D12, SPEC §10): expired rows, then inactive accounts warned and deleted."""

from __future__ import annotations

import datetime as dt
import logging

import pytest
from sqlalchemy import func, select

from agaura.models import (
    Assembly,
    AuditEntry,
    EmailToken,
    EmailTokenKind,
    OutboxEmail,
    User,
    UserSession,
    utcnow,
)
from agaura.services import demo, polls, retention
from agaura.tasks import apply_retention
from tests.conftest import World

NOW = dt.datetime(2026, 11, 5, 12, 0, tzinfo=dt.UTC)
DAY = dt.timedelta(days=1)
LIMIT = retention.INACTIVITY_LIMIT
NOTICE = retention.INACTIVITY_NOTICE
BASE_URL = "https://agaura.example"


def set_fields(world: World, user: User, **fields: object) -> None:
    with world.database.write() as db:
        row = db.get(User, user.id)
        assert row is not None
        for name, value in fields.items():
            setattr(row, name, value)


def outbox(world: World) -> list[OutboxEmail]:
    with world.database.read() as db:
        return list(db.scalars(select(OutboxEmail).order_by(OutboxEmail.id)))


def exists(world: World, user: User) -> bool:
    with world.database.read() as db:
        return db.get(User, user.id) is not None


def warn(world: World, now: dt.datetime = NOW) -> int:
    with world.database.write() as db:
        return retention.warn_inactive(db, BASE_URL, now=now)


def due(world: World, now: dt.datetime = NOW) -> list[int]:
    found = []
    with world.database.write() as db:
        while (user := retention.next_inactive(db, now=now)) is not None:
            retention.delete_inactive(db, user, now=now)
            found.append(user.id)
    return found


class TestPurgeExpired:
    def test_logins_links_and_emails(self, world: World, owner: User) -> None:
        def token(name: str, *, expires: dt.datetime, used: bool = False) -> EmailToken:
            return EmailToken(
                user_id=owner.id,
                kind=EmailTokenKind.RESET,
                token_hash=name.ljust(64, "0"),
                expires_at=expires,
                used_at=NOW - DAY if used else None,
            )

        def email(**fields: dt.datetime) -> OutboxEmail:
            return OutboxEmail(to_address="a@example.org", subject="s", body_text="b", **fields)

        with world.database.write() as db:
            db.add_all(
                [
                    UserSession(user_id=owner.id, token_hash="a" * 64, expires_at=NOW - DAY),
                    UserSession(user_id=owner.id, token_hash="b" * 64, expires_at=NOW + DAY),
                    token("used", expires=NOW + DAY, used=True),
                    token("lately", expires=NOW - 29 * DAY),  # still says "expired"
                    token("long", expires=NOW - 31 * DAY),
                    token("valid", expires=NOW + DAY),
                    email(sent_at=NOW - 31 * DAY),
                    email(sent_at=NOW - 29 * DAY),
                    email(failed_at=NOW - 91 * DAY),
                    email(failed_at=NOW - 89 * DAY),
                    email(next_attempt_at=NOW - 400 * DAY),  # still to send: never purged
                ]
            )
        with world.database.write() as db:
            purged = retention.purge_expired(db, now=NOW)
        assert purged == retention.Purged(logins=1, links=2, emails=2)
        with world.database.read() as db:
            assert db.scalars(select(UserSession.token_hash)).all() == ["b" * 64]
            kept = sorted(t[:5].rstrip("0") for t in db.scalars(select(EmailToken.token_hash)))
            assert kept == ["latel", "valid"]
            assert db.scalar(select(func.count()).select_from(OutboxEmail)) == 3


class TestWarning:
    def test_warned_thirty_days_before_three_years(self, world: World, owner: User) -> None:
        set_fields(world, owner, last_seen_at=NOW - LIMIT + NOTICE + DAY, display_name="Léa")
        assert warn(world) == 0
        set_fields(world, owner, last_seen_at=NOW - LIMIT + NOTICE)
        assert warn(world) == 1
        (sent,) = outbox(world)
        assert sent.to_address == owner.email
        assert sent.subject == "Votre compte AGaura va être supprimé"
        assert "Bonjour Léa," in sent.body_text
        assert "le 5 décembre 2026" in sent.body_text  # three years after the latest activity
        assert f"{BASE_URL}/login" in sent.body_text
        assert warn(world, NOW + DAY) == 0  # once
        with world.database.read() as db:
            entry = db.scalar(select(AuditEntry).where(AuditEntry.user_id == owner.id))
            assert entry is not None and entry.action == "account.inactivity_warned"
            assert entry.actor == "system" and entry.details == {"deletion": "2026-12-05"}

    def test_a_late_warning_still_gives_thirty_days(self, world: World, owner: User) -> None:
        """The application stopped for a while, or an account created long ago and never used:
        its creation counts as its activity."""
        set_fields(world, owner, created_at=NOW - 5 * LIMIT)
        assert warn(world) == 1
        assert "le 5 décembre 2026" in outbox(world)[0].body_text

    def test_activity_after_the_warning_cancels_it(self, world: World, owner: User) -> None:
        set_fields(world, owner, last_seen_at=NOW - LIMIT)
        assert warn(world) == 1
        set_fields(world, owner, last_seen_at=NOW + DAY)  # logged in after the email
        assert due(world, NOW + NOTICE) == []
        assert warn(world, NOW + LIMIT - NOTICE) == 0
        assert warn(world, NOW + DAY + LIMIT - NOTICE) == 1  # three years later, again

    def test_left_to_the_operator(self, world: World) -> None:
        old = NOW - 2 * LIMIT
        superadmin = world.user("admin@example.org")
        disabled = world.user("disabled@example.org")
        pending = world.user("pending@example.org")
        set_fields(world, superadmin, is_superadmin=True, created_at=old)
        set_fields(world, disabled, disabled_at=old, created_at=old)
        set_fields(world, pending, password_hash=None, created_at=old)
        with world.database.write() as db:
            visitor = demo.start(db, world.keyring, limit=1)
        set_fields(world, visitor, created_at=old, last_seen_at=old)
        assert warn(world) == 0
        assert outbox(world) == []


class TestDeletion:
    def test_thirty_days_after_the_warning(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False)
        set_fields(world, owner, last_seen_at=NOW - LIMIT + NOTICE)
        warn(world)
        assert due(world, NOW + NOTICE - DAY) == []
        assert due(world, NOW + NOTICE) == [owner.id]
        assert not exists(world, owner)
        with world.database.read() as db:
            assert db.scalar(select(Assembly).where(Assembly.public_id == public_id)) is None
            entry = db.scalar(select(AuditEntry).where(AuditEntry.action == "account.deleted"))
            assert entry is not None and entry.actor == "system"
            assert entry.details == {"assemblies": 1, "inactive_days": 1095}
            assert db.scalar(select(func.count()).select_from(OutboxEmail)) == 0

    def test_never_without_a_warning(self, world: World, owner: User) -> None:
        set_fields(world, owner, last_seen_at=NOW - 2 * LIMIT)
        assert due(world, NOW) == []

    def test_never_in_session(self, world: World, owner: User) -> None:
        world.assembly(owner)  # in session for three years: left alone
        set_fields(world, owner, last_seen_at=NOW - LIMIT)
        warn(world)
        assert due(world, NOW + NOTICE) == []

    def test_waits_while_a_poll_of_a_real_session_is_open(self, world: World, owner: User) -> None:
        set_fields(world, owner, last_seen_at=NOW - LIMIT)
        warn(world)
        other = world.user("tribune@example.org")
        poll_id = world.poll(other, world.assembly(other))
        assert due(world, NOW + NOTICE) == []
        with world.database.write() as db:
            polls.close_poll(db, other, poll_id)
        assert due(world, NOW + NOTICE) == [owner.id]

    def test_an_invitation_never_accepted(self, world: World) -> None:
        pending = world.user("pending@example.org")
        set_fields(world, pending, password_hash=None, created_at=NOW - LIMIT)
        with world.database.write() as db:
            db.add(
                EmailToken(
                    user_id=pending.id,
                    kind=EmailTokenKind.INVITE,
                    token_hash="c" * 64,
                    expires_at=NOW + DAY,
                )
            )
        assert due(world, NOW) == []  # sent again lately: its link still works
        assert due(world, NOW + DAY) == [pending.id]


def test_deletion_date() -> None:
    user = User(email="a@example.org", created_at=NOW - 4 * LIMIT, last_seen_at=NOW - LIMIT + DAY)
    assert retention.deletion_date(user, NOW) == NOW + NOTICE
    user.last_seen_at = NOW - LIMIT + 2 * NOTICE
    assert retention.deletion_date(user, NOW) == NOW + 2 * NOTICE


def test_the_hourly_job(world: World, owner: User, caplog: pytest.LogCaptureFixture) -> None:
    long_ago = utcnow() - LIMIT - NOTICE - DAY
    set_fields(world, owner, last_seen_at=long_ago, inactivity_warned_at=long_ago + DAY)
    with caplog.at_level(logging.INFO, logger="agaura.tasks"):
        assert apply_retention(world.database, BASE_URL) == 1
        assert apply_retention(world.database, BASE_URL) == 0
    assert not exists(world, owner)
    assert "1 inactive accounts warned" not in caplog.text
    assert "0 inactive accounts warned, 1 deleted" in caplog.text
    assert len(caplog.records) == 1  # nothing to say the second time
