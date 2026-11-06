"""Login sessions: 12 h, renewed while in use up to 7 days after the login, refused for disabled
accounts (FR-03)."""

import datetime as dt

from sqlalchemy import select

from agaura.db import Database
from agaura.models import AuditEntry, User, UserSession, utcnow
from agaura.security import hash_password
from agaura.services import accounts, auth

PASSWORD = "correct horse battery"


def make_user(database: Database) -> User:
    with database.write() as db:
        return accounts.create_user(db, "a@example.org", PASSWORD, verified=True)


def test_session_is_renewed_while_in_use(database: Database) -> None:
    user = make_user(database)
    start = utcnow()
    with database.write() as db:
        token = auth.start_session(db, user, user_agent="x" * 400, now=start)
        stored = db.scalar(select(UserSession))
        assert stored is not None and len(stored.user_agent or "") == 300
    with database.read() as db:
        fresh = auth.lookup(db, token, now=start + dt.timedelta(hours=1))
        assert fresh is not None and fresh.needs_touch is False
        later = start + dt.timedelta(hours=7)
        assert auth.lookup(db, token, now=later).needs_touch  # type: ignore[union-attr]
    with database.write() as db:
        auth.touch(db, token, now=later)
        auth.touch(db, "unknown-token", now=later)  # ignored
    with database.read() as db:
        # Still valid 12 h after the renewal, i.e. 19 h after logging in.
        assert auth.lookup(db, token, now=start + dt.timedelta(hours=18)) is not None
        assert auth.lookup(db, token, now=start + dt.timedelta(hours=20)) is None
        assert auth.lookup(db, None) is None


def test_session_ends_7_days_after_the_login_even_in_use(database: Database) -> None:
    user = make_user(database)
    start = utcnow()
    with database.write() as db:
        token = auth.start_session(db, user, now=start)
    # Used every 10 h, day and night (a stolen token, a console left open): renewed each time.
    now = start
    while now + dt.timedelta(hours=10) < start + auth.SESSION_MAX_AGE:
        now += dt.timedelta(hours=10)
        with database.read() as db:
            found = auth.lookup(db, token, now=now)
            assert found is not None
            due = found.needs_touch
        if due:
            with database.write() as db:
                auth.touch(db, token, now=now)
    with database.read() as db:
        stored = db.scalar(select(UserSession))
        assert stored is not None
        # Never renewed beyond the absolute end.
        assert stored.expires_at == start + auth.SESSION_MAX_AGE
        just_before = start + auth.SESSION_MAX_AGE - dt.timedelta(minutes=1)
        last = auth.lookup(db, token, now=just_before)
        # Nothing left to renew: no write on every request of the last hours.
        assert last is not None and last.needs_touch is False
        assert auth.lookup(db, token, now=start + auth.SESSION_MAX_AGE) is None


def test_session_older_than_7_days_is_refused_whatever_its_expiry(database: Database) -> None:
    """A row renewed by an earlier version, without the absolute limit, ends all the same."""
    user = make_user(database)
    now = utcnow()
    with database.write() as db:
        token = auth.start_session(db, user, now=now - dt.timedelta(days=7, minutes=1))
        stored = db.scalar(select(UserSession))
        assert stored is not None
        stored.expires_at = now + dt.timedelta(hours=11)  # used a moment ago
    with database.read() as db:
        assert auth.lookup(db, token, now=now) is None


def test_disabled_accounts_cannot_log_in_or_stay_logged_in(database: Database) -> None:
    user = make_user(database)
    with database.write() as db:
        token = auth.start_session(db, user)
        db.get(User, user.id).disabled_at = utcnow()  # type: ignore[union-attr]
    with database.write() as db:
        assert auth.lookup(db, token) is None
        assert auth.authenticate(db, "a@example.org", PASSWORD) is None
        reasons = [entry.details.get("reason") for entry in db.scalars(select(AuditEntry))]
    assert "disabled" in reasons


def test_failed_logins_are_audited_without_the_email(database: Database) -> None:
    make_user(database)
    with database.write() as db:
        assert auth.authenticate(db, "nobody@example.org", PASSWORD) is None
        assert auth.authenticate(db, "a@example.org", "wrong password") is None
        assert auth.authenticate(db, " A@example.org ", PASSWORD) is not None
        entries = list(db.scalars(select(AuditEntry).order_by(AuditEntry.id)))
    actions = [(entry.action, entry.details.get("reason")) for entry in entries]
    assert actions[-3:] == [
        ("account.login_failed", "unknown_email"),
        ("account.login_failed", "password"),
        ("account.login", None),
    ]
    assert all("nobody@example.org" not in str(entry.details) for entry in entries)


def test_login_steps_read_the_account_again_before_logging_in(database: Database) -> None:
    """The web login checks the password outside any transaction (``check_login``): a password
    changed or an account disabled in between refuses the login."""
    user = make_user(database)
    with database.read() as db:
        attempt = auth.check_login(auth.find_login(db, "a@example.org"), PASSWORD)
    assert attempt.failure is None
    with database.write() as db:
        account = db.get(User, user.id)
        assert account is not None
        account.password_hash = hash_password("another long passphrase")
    with database.write() as db:
        assert auth.record_login(db, attempt) is None
    with database.read() as db:
        attempt = auth.check_login(auth.find_login(db, "a@example.org"), "another long passphrase")
    with database.write() as db:
        db.get(User, user.id).disabled_at = utcnow()  # type: ignore[union-attr]
    with database.write() as db:
        assert auth.record_login(db, attempt) is None
        accounts.set_disabled(db, "a@example.org", False)
    with database.write() as db:
        assert auth.record_login(db, attempt) is not None
        reasons = [
            entry.details.get("reason")
            for entry in db.scalars(select(AuditEntry).order_by(AuditEntry.id))
            if entry.action.startswith("account.login")
        ]
    assert reasons == ["password", "disabled", None]


def test_login_steps_for_unknown_and_pending_addresses(database: Database) -> None:
    with database.write() as db:
        accounts.create_user(db, "p@example.org", PASSWORD).password_hash = None  # invited
    with database.read() as db:
        unknown = auth.check_login(auth.find_login(db, "nobody@example.org"), PASSWORD)
        invited = auth.check_login(auth.find_login(db, "p@example.org"), PASSWORD)
    assert (unknown.failure, invited.failure) == ("unknown_email", "pending")
    with database.write() as db:
        assert auth.record_login(db, unknown) is None
        assert auth.record_login(db, invited) is None
