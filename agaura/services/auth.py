"""Login and server-side login sessions (FR-03).

A login session is a random token in a cookie; only its hash is stored. Sessions last 12 h and
are renewed while in use, never beyond 7 days after the login (``SESSION_MAX_AGE``); logging
out deletes the row, "log out everywhere" deletes them all, together with the email links that
would let someone else back in (``TAKEOVER_LINKS``).

A web login checks the password outside any transaction: ``find_login`` reads the account in a
short read transaction, ``check_login`` runs argon2 (about 45 ms, ``agaura.security``), and
``record_login`` writes the outcome. Argon2 under the write lock would make every vote wait
behind every login attempt.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from agaura.models import EmailToken, EmailTokenKind, User, UserSession, utcnow
from agaura.security import hash_password, hash_token, new_token, verify_password
from agaura.services import audit

SESSION_LIFETIME = timedelta(hours=12)  # without activity
# From the login, whatever the activity: a stolen token stops working within a week even if it
# is used all along (or a console stays open). A week covers the preparation and a congress
# spread over several days from one login; idle sessions end after 12 h anyway.
SESSION_MAX_AGE = timedelta(days=7)
RENEW_WHEN_LEFT = timedelta(hours=6)
# A demo account (FR-09a) lives as long as its login: 2 h without activity, 24 h at most; it is
# then deleted (``agaura.services.demo``). Renewed at most every 15 minutes of activity.
DEMO_SESSION_LIFETIME = timedelta(hours=2)
DEMO_SESSION_MAX_AGE = timedelta(hours=24)
DEMO_RENEW_WHEN_LEFT = timedelta(hours=1, minutes=45)
LAST_SEEN_PRECISION = timedelta(days=1)
MAX_USER_AGENT_LENGTH = 300

# Verifying against a real hash when the email is unknown keeps response times identical.
_DUMMY_HASH = hash_password("timing-equalizer-password")

# Email links that hand the account to whoever opens them: a password reset, and an email
# change (the new address then receives the reset links). Someone who once learned the
# password can request an email change to their own mailbox; logging out everywhere, changing
# or resetting the password void these links, so each of these actions locks that person out.
# Verification links stay: they only confirm the current address.
TAKEOVER_LINKS = (EmailTokenKind.RESET, EmailTokenKind.CHANGE_EMAIL)


@dataclass(frozen=True, slots=True)
class LoginAttempt:
    """A login on its way from ``find_login`` through ``check_login`` to ``record_login``."""

    user_id: int | None
    password_hash: str | None  # the stored hash; None for an unknown address or an invitation
    failure: str | None = None  # reason for the audit log, once known


def find_login(db: Session, email: str) -> LoginAttempt:
    """The account behind a typed address, as plain data for ``check_login``."""
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        return LoginAttempt(None, None, "unknown_email")
    if user.password_hash is None:  # invitation not accepted yet: no password to match
        return LoginAttempt(user.id, None, "pending")
    return LoginAttempt(user.id, user.password_hash)


def check_login(attempt: LoginAttempt, password: str) -> LoginAttempt:
    """Verify the password; no database access, so that it runs outside any transaction."""
    if attempt.password_hash is None:
        verify_password(_DUMMY_HASH, password)
        return attempt
    if not verify_password(attempt.password_hash, password):
        return replace(attempt, failure="password")
    return attempt


def record_login(db: Session, attempt: LoginAttempt, *, now: datetime | None = None) -> User | None:
    """Log the attempt (FR-81) and return the account if it may log in. The account is read
    again: a password changed, or the account disabled or deleted, since ``find_login`` refuses
    the login."""
    now = now or utcnow()
    user = db.get(User, attempt.user_id) if attempt.user_id is not None else None
    failure = attempt.failure
    if failure is None:
        if user is None:
            failure = "unknown_email"
        elif user.password_hash != attempt.password_hash:
            failure = "password"
        elif user.disabled_at is not None:
            failure = "disabled"
    if failure is not None or user is None:
        audit.record(
            db,
            "account.login_failed",
            user_id=user.id if user is not None else None,
            details={"reason": failure},
            now=now,
        )
        return None
    audit.record(db, "account.login", user_id=user.id, now=now)
    return user


def authenticate(
    db: Session, email: str, password: str, *, now: datetime | None = None
) -> User | None:
    """The account matching these credentials, or None; failures are logged (FR-81).

    All three steps in the caller's transaction (command line, tests); the login page splits
    them to keep argon2 out of the write transaction.
    """
    return record_login(db, check_login(find_login(db, email), password), now=now)


def start_session(
    db: Session, user: User, *, user_agent: str | None = None, now: datetime | None = None
) -> str:
    """Create a login session and return the token for the cookie."""
    now = now or utcnow()
    token = new_token()
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=_expiry(now, now, demo=user.is_demo),
            created_at=now,
            user_agent=(user_agent or "")[:MAX_USER_AGENT_LENGTH] or None,
        )
    )
    account = db.get(User, user.id)  # the caller may hold a detached copy
    if account is not None:
        account.last_seen_at = now
    db.flush()
    return token


def _expiry(created_at: datetime, now: datetime, *, demo: bool) -> datetime:
    """The end of a session renewed at ``now``: 12 h later, at most ``SESSION_MAX_AGE`` after
    the login (2 h and 24 h for a demo account)."""
    if demo:
        return min(now + DEMO_SESSION_LIFETIME, created_at + DEMO_SESSION_MAX_AGE)
    return min(now + SESSION_LIFETIME, created_at + SESSION_MAX_AGE)


@dataclass(frozen=True, slots=True)
class SessionLookup:
    user: User
    needs_touch: bool  # renewal or last-seen update due: call ``touch`` in a write transaction


def lookup(db: Session, token: str | None, *, now: datetime | None = None) -> SessionLookup | None:
    """The account behind a login cookie, if the session is valid and the account enabled."""
    if not token:
        return None
    now = now or utcnow()
    row = db.execute(
        select(UserSession, User)
        .join(User, UserSession.user_id == User.id)
        .where(
            UserSession.token_hash == hash_token(token),
            UserSession.expires_at > now,
            # Checked as well: a row renewed by an earlier version, without this limit, may end
            # later.
            UserSession.created_at > now - SESSION_MAX_AGE,
        )
    ).first()
    if row is None:
        return None
    login, user = row
    if user.disabled_at is not None:
        return None
    # No renewal once the end is the absolute one: every request would write for nothing.
    renew_when_left = DEMO_RENEW_WHEN_LEFT if user.is_demo else RENEW_WHEN_LEFT
    renewal_due = (
        login.expires_at - now < renew_when_left
        and _expiry(login.created_at, now, demo=user.is_demo) > login.expires_at
    )
    needs_touch = renewal_due or (
        user.last_seen_at is None or now - user.last_seen_at > LAST_SEEN_PRECISION
    )
    return SessionLookup(user=user, needs_touch=needs_touch)


def touch(db: Session, token: str, *, now: datetime | None = None) -> None:
    """Extend the session, never beyond ``SESSION_MAX_AGE`` after the login, and record
    activity (inactive accounts are purged after 3 years)."""
    now = now or utcnow()
    login = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
    if login is None:
        return
    user = db.get(User, login.user_id)
    if user is None:
        return
    login.expires_at = _expiry(login.created_at, now, demo=user.is_demo)
    user.last_seen_at = now


def end_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(UserSession).where(UserSession.token_hash == hash_token(token)))


def void_takeover_links(db: Session, user: User) -> None:
    """Void the pending links of ``TAKEOVER_LINKS`` (used ones go too)."""
    db.execute(
        delete(EmailToken).where(EmailToken.user_id == user.id, EmailToken.kind.in_(TAKEOVER_LINKS))
    )


def end_all_sessions(db: Session, user: User, *, now: datetime | None = None) -> None:
    """Log out on every device and void the takeover links; also used after a password
    reset."""
    db.execute(delete(UserSession).where(UserSession.user_id == user.id))
    void_takeover_links(db, user)
    audit.record(db, "account.logged_out_everywhere", user_id=user.id, now=now)
