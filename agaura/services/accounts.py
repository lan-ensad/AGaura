"""Accounts: creation, quotas, email links, settings, deletion, super-admin and operator actions.

Email links (verification, password reset, email change, invitation) carry a random token; only
its hash is stored, each link works once, and issuing a new link of a kind voids the previous
ones. Every event on an account is written to the audit log (FR-81), never with a password or a
token.

The super-admin (created on the command line only) invites and deletes accounts and nothing
else: it never owns a session and these functions never return anything about sessions. An
invited account has no password (``password_hash`` is None) until the person opens the link.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from email.utils import parseaddr
from enum import StrEnum

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from agaura.db import compact_after_commit
from agaura.models import (
    Assembly,
    AssemblyStatus,
    AuditEntry,
    EmailToken,
    EmailTokenKind,
    OutboxEmail,
    User,
    UserSession,
    utcnow,
)
from agaura.security import hash_password, hash_token, new_token, verify_password
from agaura.services import audit, auth, emails
from agaura.services.audit import Actor
from agaura.services.errors import InvalidInputError, InvalidStateError, NotFoundError

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256  # argon2 accepts more, but hashing megabytes would be a cheap DoS
MAX_EMAIL_LENGTH = 254
MAX_DISPLAY_NAME_LENGTH = 100
MAX_TOKEN_LENGTH = 100

TOKEN_LIFETIMES = {
    EmailTokenKind.VERIFY: timedelta(hours=24),
    EmailTokenKind.RESET: timedelta(hours=1),
    EmailTokenKind.CHANGE_EMAIL: timedelta(hours=24),
    EmailTokenKind.INVITE: timedelta(days=7),
}
ACCOUNT_LOG_LIMIT = 100

# Audit actor of the super-admin's actions (web interface), next to ``audit.Actor``.
SUPERADMIN_ACTOR = "superadmin"


@dataclass(frozen=True, slots=True)
class Quotas:
    """Per-account limits (FR-08); the operator can override them per account."""

    open_assemblies: int = 20  # sessions not yet closed
    polls_per_assembly: int = 200
    codes_per_assembly: int = 2000


QUOTA_NAMES = tuple(field.name for field in fields(Quotas))
# A demo account (FR-09a): enough to try every feature, too little to run a real assembly on
# (its sessions take 5 voters at most, ``access.DEMO_VOTERS``).
DEMO_QUOTAS = Quotas(open_assemblies=3, polls_per_assembly=20, codes_per_assembly=20)


def quotas(user: User) -> Quotas:
    if user.is_demo:
        return DEMO_QUOTAS
    overrides = {
        name: value
        for name, value in (user.quota_overrides or {}).items()
        if name in QUOTA_NAMES and isinstance(value, int) and not isinstance(value, bool)
    }
    return replace(Quotas(), **overrides)


# List separators, delimiters of display names, comments and domain literals, and quoting: legal
# in some addresses, never needed here, and the way to slip a second recipient into a header.
EMAIL_FORBIDDEN_CHARACTERS = frozenset(',;<>()[]"\\')


def _unsafe_in_email(character: str) -> bool:
    """Whitespace, control and invisible characters (Unicode categories C and Z), or one of
    ``EMAIL_FORBIDDEN_CHARACTERS``."""
    return (
        character in EMAIL_FORBIDDEN_CHARACTERS
        or character.isspace()
        or unicodedata.category(character)[0] in "CZ"
    )


def normalize_email(raw: str) -> str:
    """The address in lower case, or ``invalid_email`` unless it is one plain ``local@domain``.

    Strict on purpose: the address becomes the To header of outgoing emails, where a line break
    would break the header and a comma would add recipients.
    """
    email = raw.strip().lower()
    local, _, domain = email.partition("@")
    if (
        not local
        or "@" in domain
        or "." not in domain
        or not all(domain.split("."))  # no empty label: "a@.org", "a@x..org"
        or len(email) > MAX_EMAIL_LENGTH
        or any(_unsafe_in_email(character) for character in email)
        or parseaddr(email) != ("", email)
    ):
        raise InvalidInputError("invalid_email")
    return email


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise InvalidInputError("password_too_short")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise InvalidInputError("password_too_long")


def clean_display_name(raw: str | None) -> str | None:
    name = " ".join((raw or "").split())
    if len(name) > MAX_DISPLAY_NAME_LENGTH:
        raise InvalidInputError("too_long")
    return name or None


def _email_taken(db: Session, email: str) -> bool:
    return db.scalar(select(User.id).where(User.email == email)) is not None


def _new_user(
    db: Session,
    email: str,
    password: str | None,
    *,
    display_name: str | None,
    verified: bool,
    superadmin: bool,
    now: datetime,
    demo: bool = False,
) -> User:
    email = normalize_email(email)
    if password is not None:
        check_password(password)
    display_name = clean_display_name(display_name)
    if _email_taken(db, email):
        raise InvalidInputError("email_taken")
    user = User(
        email=email,
        password_hash=None if password is None else hash_password(password),
        display_name=display_name,
        email_verified_at=now if verified else None,
        created_at=now,
        is_superadmin=superadmin,
        is_demo=demo,
    )
    db.add(user)
    db.flush()
    return user


def create_user(
    db: Session,
    email: str,
    password: str,
    *,
    display_name: str | None = None,
    verified: bool = False,
    superadmin: bool = False,
    now: datetime | None = None,
) -> User:
    return _new_user(
        db,
        email,
        password,
        display_name=display_name,
        verified=verified,
        superadmin=superadmin,
        now=now or utcnow(),
    )


def _attached(db: Session, user: User) -> User:
    """The account as a row of this transaction (web pages hold a detached copy)."""
    account = db.get(User, user.id)
    if account is None:
        raise NotFoundError("user")
    return account


def _require_password(user: User, password: str) -> None:
    if user.password_hash is None or not verify_password(user.password_hash, password):
        raise InvalidInputError("wrong_password")


class AccountState(StrEnum):
    PENDING = "pending"  # invited, no password chosen yet
    ACTIVE = "active"
    DISABLED = "disabled"  # by the operator


def account_state(user: User) -> AccountState:
    if user.disabled_at is not None:
        return AccountState.DISABLED
    if user.password_hash is None:
        return AccountState.PENDING
    return AccountState.ACTIVE


# --- Email links ----------------------------------------------------------------------------


def issue_token(
    db: Session,
    user: User,
    kind: EmailTokenKind,
    *,
    new_email: str | None = None,
    now: datetime | None = None,
) -> str:
    """Create a single-use link token; earlier links of the same kind stop working."""
    now = now or utcnow()
    _void_tokens(db, user, kind)
    token = new_token()
    db.add(
        EmailToken(
            user_id=user.id,
            kind=kind,
            token_hash=hash_token(token),
            new_email=new_email,
            expires_at=now + TOKEN_LIFETIMES[kind],
        )
    )
    db.flush()
    return token


def _void_tokens(db: Session, user: User, *kinds: EmailTokenKind) -> None:
    db.execute(delete(EmailToken).where(EmailToken.user_id == user.id, EmailToken.kind.in_(kinds)))


def _find_token(
    db: Session, kind: EmailTokenKind, token: str, now: datetime
) -> tuple[EmailToken, User]:
    row = None
    if token and len(token) <= MAX_TOKEN_LENGTH:
        row = db.scalar(
            select(EmailToken).where(
                EmailToken.token_hash == hash_token(token), EmailToken.kind == kind
            )
        )
    user = db.get(User, row.user_id) if row is not None else None
    if row is None or row.used_at is not None or user is None or user.disabled_at is not None:
        raise InvalidInputError("invalid_link")
    if row.expires_at <= now:
        raise InvalidInputError("expired_link")
    return row, user


def check_token(
    db: Session, kind: EmailTokenKind, token: str, *, now: datetime | None = None
) -> User:
    """The account behind a valid, unused link, without using it up."""
    return _find_token(db, kind, token, now or utcnow())[1]


def _consume_token(
    db: Session, kind: EmailTokenKind, token: str, now: datetime
) -> tuple[EmailToken, User]:
    row, user = _find_token(db, kind, token, now)
    row.used_at = now
    return row, user


def pending_email_change(db: Session, user: User, *, now: datetime | None = None) -> str | None:
    """The new address waiting for confirmation, if a change link is still valid."""
    return db.scalar(
        select(EmailToken.new_email).where(
            EmailToken.user_id == user.id,
            EmailToken.kind == EmailTokenKind.CHANGE_EMAIL,
            EmailToken.used_at.is_(None),
            EmailToken.expires_at > (now or utcnow()),
        )
    )


# --- Signup and verification (FR-01, FR-02) -------------------------------------------------


def register(
    db: Session,
    email: str,
    password: str,
    *,
    display_name: str | None = None,
    now: datetime | None = None,
) -> tuple[User, str]:
    """Self-service signup: the new account and the token of its verification link."""
    now = now or utcnow()
    user = create_user(db, email, password, display_name=display_name, now=now)
    audit.record(db, "account.created", user_id=user.id, now=now)
    return user, issue_token(db, user, EmailTokenKind.VERIFY, now=now)


def request_verification(
    db: Session, user: User, *, now: datetime | None = None
) -> tuple[User, str] | None:
    """The account and a new verification link token; None if already confirmed."""
    account = _attached(db, user)
    if account.email_verified_at is not None:
        return None
    return account, issue_token(db, account, EmailTokenKind.VERIFY, now=now)


def verify_email(db: Session, token: str, *, now: datetime | None = None) -> User:
    now = now or utcnow()
    _row, user = _consume_token(db, EmailTokenKind.VERIFY, token, now)
    if user.email_verified_at is None:
        user.email_verified_at = now
        audit.record(db, "account.email_verified", user_id=user.id, now=now)
    return user


# --- Password reset (FR-04) -----------------------------------------------------------------


def request_password_reset(
    db: Session, email: str, *, now: datetime | None = None
) -> tuple[User, str] | None:
    """The account and its reset link token; None for an unknown or disabled address, or an
    invitation not accepted yet (the invitation link is the way in).

    Callers must answer the same way in every case, so the form never reveals who has an
    account.
    """
    now = now or utcnow()
    user = db.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None or account_state(user) is not AccountState.ACTIVE:
        return None
    token = issue_token(db, user, EmailTokenKind.RESET, now=now)
    audit.record(db, "account.password_reset_requested", user_id=user.id, now=now)
    return user, token


def reset_password(db: Session, token: str, password: str, *, now: datetime | None = None) -> User:
    """Set a new password through a reset link, then log out every device; pending reset and
    email change links stop working (``auth.end_all_sessions``)."""
    now = now or utcnow()
    check_password(password)
    _row, user = _consume_token(db, EmailTokenKind.RESET, token, now)
    user.password_hash = hash_password(password)
    if user.email_verified_at is None:  # receiving the link proves the address
        user.email_verified_at = now
    audit.record(db, "account.password_reset", user_id=user.id, now=now)
    auth.end_all_sessions(db, user, now=now)
    return user


# --- Account settings (FR-03, FR-05) --------------------------------------------------------


def update_display_name(
    db: Session, user: User, display_name: str | None, *, now: datetime | None = None
) -> User:
    account = _attached(db, user)
    name = clean_display_name(display_name)
    if name != account.display_name:
        account.display_name = name
        audit.record(db, "account.display_name_changed", user_id=account.id, now=now)
    return account


def change_password(
    db: Session,
    user: User,
    current_password: str,
    new_password: str,
    *,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> str:
    """Change the password and end every login session, the current one included; pending
    reset and email change links stop working.

    Returns the token of a new login session for the current device: a copy of its previous
    cookie, stolen or left on another device, stops working too.
    """
    account = _attached(db, user)
    _require_password(account, current_password)
    check_password(new_password)
    account.password_hash = hash_password(new_password)
    db.execute(delete(UserSession).where(UserSession.user_id == account.id))
    auth.void_takeover_links(db, account)
    audit.record(db, "account.password_changed", user_id=account.id, now=now)
    return auth.start_session(db, account, user_agent=user_agent, now=now)


def request_email_change(
    db: Session, user: User, password: str, new_email: str, *, now: datetime | None = None
) -> tuple[str, str | None]:
    """The normalized new address and the token of the link to send to it (FR-05); no token
    when the address already belongs to an account.

    A notice goes to the current address at once, so that the account holder learns of a
    change asked by someone else while it can still be cancelled (password change or reset,
    logging out everywhere: ``auth.TAKEOVER_LINKS``).

    An address that already has an account receives an email saying so instead of the link
    (queued here), and the change shows as pending all the same: callers answer the same way
    in both cases, so the form never reveals who has an account.
    """
    account = _attached(db, user)
    _require_password(account, password)
    email = normalize_email(new_email)
    if email == account.email:
        raise InvalidInputError("same_email")
    owner = db.scalar(select(User).where(User.email == email))
    # Issued in both cases; the link of a taken address is never sent, and ``change_email``
    # would refuse it anyway.
    token = issue_token(db, account, EmailTokenKind.CHANGE_EMAIL, new_email=email, now=now)
    emails.queue(db, account.email, emails.email_change_requested(account, email), now=now)
    if owner is not None:
        emails.queue(db, email, emails.address_in_use(owner), now=now)
    audit.record(db, "account.email_change_requested", user_id=account.id, now=now)
    return email, None if owner is not None else token


def change_email(db: Session, token: str, *, now: datetime | None = None) -> tuple[User, str]:
    """Apply an email change confirmed from the new address; returns the account and its
    previous address."""
    now = now or utcnow()
    row, user = _consume_token(db, EmailTokenKind.CHANGE_EMAIL, token, now)
    new_email = row.new_email or ""
    if not new_email or _email_taken(db, new_email):
        raise InvalidInputError("email_unavailable")
    previous = user.email
    user.email = new_email
    user.email_verified_at = now
    # Links sent to the previous address must not work any more.
    _void_tokens(db, user, EmailTokenKind.VERIFY, EmailTokenKind.RESET)
    audit.record(db, "account.email_changed", user_id=user.id, now=now)
    return user, previous


def account_log(db: Session, user: User, *, limit: int = ACCOUNT_LOG_LIMIT) -> list[AuditEntry]:
    """Audit entries about the account itself, most recent first (FR-84)."""
    return list(
        db.scalars(
            select(AuditEntry)
            .where(AuditEntry.user_id == user.id, AuditEntry.action.startswith("account."))
            .order_by(AuditEntry.id.desc())
            .limit(limit)
        )
    )


# --- Deletion (FR-06) -----------------------------------------------------------------------


def delete_account(db: Session, user: User, password: str, *, now: datetime | None = None) -> None:
    """Delete one's own account with all its sessions (FR-06); refused while one of them is in
    session. A super-admin is removed on the command line only."""
    account = _attached(db, user)
    _require_password(account, password)
    if account.is_superadmin:
        raise InvalidStateError("superadmin_protected")
    _delete_with_sessions(db, account, actor=Actor.USER, details={}, now=now)


def _delete_with_sessions(
    db: Session,
    account: User,
    *,
    actor: str,
    details: dict[str, int],
    now: datetime | None,
    in_session_too: bool = False,
) -> int:
    """Delete an account and its sessions; returns how many sessions went with it. Refused
    while one of them is in session, unless ``in_session_too`` (a demo account, FR-09a).

    The database cascade removes sessions, polls, ballots, login sessions and email links. The
    audit log keeps its entries (no foreign key), so the hash chain stays intact.
    """
    statuses = dict(
        db.execute(
            select(Assembly.status, func.count())
            .where(Assembly.owner_id == account.id)
            .group_by(Assembly.status)
        ).all()
    )
    if statuses.get(AssemblyStatus.IN_SESSION) and not in_session_too:
        raise InvalidStateError("account_in_session")
    addresses = {account.email} | {
        email
        for email in db.scalars(
            select(EmailToken.new_email).where(EmailToken.user_id == account.id)
        )
        if email
    }
    count = sum(statuses.values())
    audit.record(
        db,
        "account.deleted",
        actor=actor,
        user_id=account.id,
        details={"assemblies": count, **details},
        now=now,
    )
    # Emails addressed to the person go with the account (outbox history included).
    db.execute(delete(OutboxEmail).where(OutboxEmail.to_address.in_(addresses)))
    db.delete(account)
    db.flush()
    if count:
        compact_after_commit(db)  # sessions, polls and ballots went with it
    return count


# --- Super-admin: invitations and deletion --------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccountRow:
    """What the super-admin sees of an account: never anything about its sessions."""

    id: int
    email: str
    display_name: str | None
    created_at: datetime
    last_seen_at: datetime | None
    state: AccountState
    is_superadmin: bool
    invitation_valid: bool  # pending accounts: the latest invitation link still works


def _superadmin(db: Session, admin: User) -> User:
    """The super-admin as a row of this transaction; anyone else gets ``NotFoundError``."""
    account = db.get(User, admin.id)
    if account is None or not account.is_superadmin or account.disabled_at is not None:
        raise NotFoundError("user")
    return account


def _managed_account(db: Session, account_id: int) -> User:
    account = db.get(User, account_id)
    if account is None or account.is_demo:
        raise NotFoundError("user")
    if account.is_superadmin:  # super-admins are managed on the server only
        raise InvalidStateError("superadmin_protected")
    return account


def list_accounts(db: Session, admin: User, *, now: datetime | None = None) -> list[AccountRow]:
    """Every account, most recent first; demo accounts (FR-09a) come and go on their own."""
    _superadmin(db, admin)
    valid_invitations = set(
        db.scalars(
            select(EmailToken.user_id).where(
                EmailToken.kind == EmailTokenKind.INVITE,
                EmailToken.used_at.is_(None),
                EmailToken.expires_at > (now or utcnow()),
            )
        )
    )
    return [
        AccountRow(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            created_at=user.created_at,
            last_seen_at=user.last_seen_at,
            state=account_state(user),
            is_superadmin=user.is_superadmin,
            invitation_valid=user.id in valid_invitations,
        )
        for user in db.scalars(
            select(User)
            .where(User.is_demo.is_(False))
            .order_by(User.created_at.desc(), User.id.desc())
        )
    ]


def invite(
    db: Session,
    admin: User,
    email: str,
    *,
    display_name: str | None = None,
    now: datetime | None = None,
) -> tuple[User, str]:
    """Create an account without a password; returns it and the token of its invitation link.

    The person chooses a password through the link (``accept_invitation``), which also confirms
    the address.
    """
    now = now or utcnow()
    admin = _superadmin(db, admin)
    user = _new_user(
        db, email, None, display_name=display_name, verified=False, superadmin=False, now=now
    )
    audit.record(
        db,
        "account.invited",
        actor=SUPERADMIN_ACTOR,
        user_id=user.id,
        details={"by": admin.id},
        now=now,
    )
    return user, issue_token(db, user, EmailTokenKind.INVITE, now=now)


def resend_invitation(
    db: Session, admin: User, account_id: int, *, now: datetime | None = None
) -> tuple[User, str]:
    """A new invitation link for an account still waiting for its password; the previous link
    stops working."""
    now = now or utcnow()
    admin = _superadmin(db, admin)
    user = _managed_account(db, account_id)
    if account_state(user) is not AccountState.PENDING:
        raise InvalidStateError("superadmin_not_pending")
    token = issue_token(db, user, EmailTokenKind.INVITE, now=now)
    audit.record(
        db,
        "account.invitation_resent",
        actor=SUPERADMIN_ACTOR,
        user_id=user.id,
        details={"by": admin.id},
        now=now,
    )
    return user, token


def accept_invitation(
    db: Session, token: str, password: str, *, now: datetime | None = None
) -> User:
    """Set the first password through an invitation link; the address is then confirmed."""
    now = now or utcnow()
    check_password(password)
    _row, user = _consume_token(db, EmailTokenKind.INVITE, token, now)
    if user.password_hash is not None:  # cannot happen through the web: the link is single use
        raise InvalidInputError("invalid_link")
    user.password_hash = hash_password(password)
    user.email_verified_at = user.email_verified_at or now
    audit.record(db, "account.invitation_accepted", user_id=user.id, now=now)
    return user


def delete_account_by_superadmin(
    db: Session,
    admin: User,
    account_id: int,
    confirm_email: str,
    *,
    password: str,
    now: datetime | None = None,
) -> None:
    """Delete an account and its sessions after the super-admin typed its address again, and
    its own password (``wrong_password``, checked first: a stolen super-admin login session
    alone deletes nothing).

    Refused for super-admins (the super-admin's own account included) and while one of the
    account's sessions is in session.
    """
    admin = _superadmin(db, admin)
    _require_password(admin, password)
    account = _managed_account(db, account_id)
    if confirm_email.strip().lower() != account.email:
        raise InvalidInputError("superadmin_confirm_mismatch")
    _delete_with_sessions(db, account, actor=SUPERADMIN_ACTOR, details={"by": admin.id}, now=now)


# --- Operator (command line) ----------------------------------------------------------------


def find_user(db: Session, email: str) -> User:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        raise NotFoundError("user")
    return user


def create_user_by_operator(
    db: Session,
    email: str,
    password: str,
    *,
    display_name: str | None = None,
    verified: bool = False,
    superadmin: bool = False,
    now: datetime | None = None,
) -> User:
    """Create an account; a super-admin's address counts as confirmed by the operator."""
    verified = verified or superadmin
    user = create_user(
        db,
        email,
        password,
        display_name=display_name,
        verified=verified,
        superadmin=superadmin,
        now=now,
    )
    details: dict[str, bool] = {"verified": verified}
    if superadmin:
        details["superadmin"] = True
    audit.record(
        db, "account.created", actor=Actor.OPERATOR, user_id=user.id, details=details, now=now
    )
    return user


def delete_account_by_operator(db: Session, email: str, *, now: datetime | None = None) -> int:
    """Delete any account (super-admins included) with its sessions; returns how many sessions
    went with it. Refused while one of them is in session."""
    return _delete_with_sessions(
        db, find_user(db, email), actor=Actor.OPERATOR, details={}, now=now
    )


def delete_inactive_account(
    db: Session, user: User, *, inactive_days: int, now: datetime | None = None
) -> int:
    """Delete an account after its warning of inactivity (D12, ``agaura.services.retention``),
    with its sessions; returns how many went with it. Refused while one is in session."""
    return _delete_with_sessions(
        db,
        _attached(db, user),
        actor=Actor.SYSTEM,
        details={"inactive_days": inactive_days},
        now=now,
    )


def set_disabled(db: Session, email: str, disabled: bool, *, now: datetime | None = None) -> User:
    """Disable an account or enable it again. Disabled, it cannot log in, its login sessions
    and delegate links end, and its sessions look unknown from outside
    (``assemblies.find_public_assembly``); nothing is deleted (FR-17a)."""
    now = now or utcnow()
    user = find_user(db, email)
    if disabled:
        user.disabled_at = user.disabled_at or now
        db.execute(delete(UserSession).where(UserSession.user_id == user.id))
    else:
        user.disabled_at = None
    action = "account.disabled" if disabled else "account.enabled"
    audit.record(db, action, actor=Actor.OPERATOR, user_id=user.id, now=now)
    return user


def set_quotas(
    db: Session,
    email: str,
    changes: dict[str, int],
    *,
    reset: bool = False,
    now: datetime | None = None,
) -> Quotas:
    """Override quotas of one account (FR-08); ``reset`` first restores the defaults."""
    user = find_user(db, email)
    for name, value in changes.items():
        if name not in QUOTA_NAMES or value < 0:
            raise InvalidInputError("invalid_quota", name)
    overrides = {} if reset else dict(user.quota_overrides or {})
    overrides.update(changes)
    user.quota_overrides = overrides  # a new dict, so the JSON column is saved
    audit.record(
        db,
        "account.quotas_changed",
        actor=Actor.OPERATOR,
        user_id=user.id,
        details={"quotas": overrides},
        now=now,
    )
    return quotas(user)
