"""Retention (D12, SPEC §10): what the application deletes by itself, demo accounts aside.

- Login sessions once expired. Email links once used, or 30 days after they expired: until
  then, opening one still says that it expired rather than that it is unknown.
- Emails of the outbox 30 days after they were sent, 90 days after they were given up (the
  operator sees the failures meanwhile, ``agaura email failed``).
- Accounts inactive for 3 years (D12): an email warning first, then the deletion with all their
  sessions, 30 days after the warning at the earliest. Activity is the latest login or request,
  or the creation for an account never used; any activity after the warning cancels it.
  Super-admins and disabled accounts are left to the operator. An invitation never accepted is
  deleted without a warning 3 years after the account was created, once its link has expired:
  no one could act on the warning.

Deleting accounts rebuilds the database file (``compact_after_commit``), which would hold the
ballots of a vote back for its duration: it waits while a poll of a real session is open.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, and_, delete, exists, func, or_, select
from sqlalchemy.orm import Session

from agaura.backups import open_polls
from agaura.models import (
    Assembly,
    AssemblyStatus,
    EmailToken,
    EmailTokenKind,
    OutboxEmail,
    User,
    UserSession,
    utcnow,
)
from agaura.services import accounts, audit, emails
from agaura.services.audit import Actor

EXPIRED_LINKS_KEPT = timedelta(days=30)
SENT_EMAILS_KEPT = timedelta(days=30)
FAILED_EMAILS_KEPT = timedelta(days=90)
INACTIVITY_LIMIT = timedelta(days=3 * 365)  # D12
INACTIVITY_NOTICE = timedelta(days=30)
DELETIONS_PER_RUN = 20  # one transaction for them all (agaura.tasks): bounded, so it stays short


@dataclass(frozen=True, slots=True)
class Purged:
    logins: int = 0
    links: int = 0
    emails: int = 0


def purge_expired(db: Session, *, now: datetime | None = None) -> Purged:
    now = now or utcnow()
    logins = db.execute(delete(UserSession).where(UserSession.expires_at <= now)).rowcount
    links = db.execute(
        delete(EmailToken).where(
            or_(EmailToken.used_at.is_not(None), EmailToken.expires_at <= now - EXPIRED_LINKS_KEPT)
        )
    ).rowcount
    sent = db.execute(
        delete(OutboxEmail).where(
            or_(
                OutboxEmail.sent_at <= now - SENT_EMAILS_KEPT,
                OutboxEmail.failed_at <= now - FAILED_EMAILS_KEPT,
            )
        )
    ).rowcount
    return Purged(logins=logins or 0, links=links or 0, emails=sent or 0)


def _activity() -> ColumnElement[datetime]:
    return func.coalesce(User.last_seen_at, User.created_at)


def _left_to_the_operator() -> ColumnElement[bool]:
    return or_(User.is_demo, User.is_superadmin, User.disabled_at.is_not(None))


def deletion_date(user: User, warned_at: datetime) -> datetime:
    """The earliest deletion of an account warned at ``warned_at``."""
    activity = user.last_seen_at or user.created_at
    return max(activity + INACTIVITY_LIMIT, warned_at + INACTIVITY_NOTICE)


def warn_inactive(db: Session, base_url: str, *, now: datetime | None = None) -> int:
    """Queue the warning to every account that reaches 3 years without activity within the
    notice and has not been warned since its latest activity; returns how many."""
    now = now or utcnow()
    activity = _activity()
    due = db.scalars(
        select(User).where(
            ~_left_to_the_operator(),
            User.password_hash.is_not(None),
            activity <= now - (INACTIVITY_LIMIT - INACTIVITY_NOTICE),
            or_(User.inactivity_warned_at.is_(None), User.inactivity_warned_at < activity),
        )
    ).all()
    for user in due:
        deleted_on = deletion_date(user, now)
        warning = emails.inactivity_warning(user, f"{base_url}/login", deleted_on)
        emails.queue(db, user.email, warning, now=now)
        user.inactivity_warned_at = now
        audit.record(
            db,
            "account.inactivity_warned",
            actor=Actor.SYSTEM,
            user_id=user.id,
            details={"deletion": deleted_on.date().isoformat()},
            now=now,
        )
    db.flush()
    return len(due)


def _due_for_deletion(now: datetime) -> ColumnElement[bool]:
    activity = _activity()
    warned = and_(
        User.password_hash.is_not(None),
        activity <= now - INACTIVITY_LIMIT,
        User.inactivity_warned_at >= activity,
        User.inactivity_warned_at <= now - INACTIVITY_NOTICE,
    )
    live_invitation = exists().where(
        EmailToken.user_id == User.id,
        EmailToken.kind == EmailTokenKind.INVITE,
        EmailToken.used_at.is_(None),
        EmailToken.expires_at > now,
    )
    never_accepted = and_(
        User.password_hash.is_(None),
        User.created_at <= now - INACTIVITY_LIMIT,
        ~live_invitation,
    )
    in_session = exists().where(
        Assembly.owner_id == User.id, Assembly.status == AssemblyStatus.IN_SESSION
    )
    return and_(~_left_to_the_operator(), or_(warned, never_accepted), ~in_session)


def next_inactive(db: Session, *, now: datetime | None = None) -> User | None:
    """One account due for deletion, or None; None too while a poll of a real session is open."""
    now = now or utcnow()
    if open_polls(db):
        return None
    return db.scalar(select(User).where(_due_for_deletion(now)).order_by(User.id).limit(1))


def delete_inactive(db: Session, user: User, *, now: datetime | None = None) -> int:
    """Delete an account found by ``next_inactive`` with its sessions; returns how many sessions
    went with it."""
    now = now or utcnow()
    activity = user.last_seen_at or user.created_at
    return accounts.delete_inactive_account(db, user, inactive_days=(now - activity).days, now=now)
