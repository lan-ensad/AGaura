"""Voter access (FR-20 to FR-28): session password, individual codes, credentials, entry lock,
access cap, headcount check, screen and public results links.

A credential is valid only while its session is in session and matches the current access
mode, so switching to codes mode mid-session (to rerun a vote one person = one vote) disables
every device that joined with the password.
"""

from __future__ import annotations

import datetime as dt
import math
import secrets
from dataclasses import dataclass
from fractions import Fraction

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import Session

from agaura.models import (
    AccessMode,
    Assembly,
    AssemblyStatus,
    BatchStatus,
    CodeBatch,
    CredentialKind,
    Participation,
    Poll,
    PollStatus,
    User,
    VoterCredential,
)
from agaura.security import (
    MIN_SESSION_PASSWORD_LENGTH,
    VOTER_CODE_LENGTH,
    Keyring,
    format_voter_code,
    hash_token,
    new_session_password,
    new_token,
    new_voter_code,
    normalize_secret,
    secrets_match,
)
from agaura.services import audit
from agaura.services.accounts import quotas
from agaura.services.assemblies import find_public_assembly, get_assembly, require_status
from agaura.services.errors import (
    AccessDeniedError,
    InvalidInputError,
    InvalidStateError,
    NotFoundError,
    QuotaExceededError,
)

MAX_SESSION_PASSWORD_LENGTH = 40
MAX_CODES_PER_BATCH = 2000
# Password mode: margin of the access cap over the people expected (FR-27), for those who
# change device.
ACCESS_CAP_MARGIN = Fraction(1, 10)
ACCESS_CAP_MIN_MARGIN = 10
ACCESS_CAP_REACHED = "access_cap_reached"
# A session of a demo account (FR-09a): 5 voters at most, in either access mode, whatever the
# member count or the headcount; past it, ``DEMO_VOTERS_FULL``.
DEMO_VOTERS = 5
DEMO_VOTERS_FULL = "demo_voters_full"

_KIND_FOR_MODE = {AccessMode.PASSWORD: CredentialKind.DEVICE, AccessMode.CODES: CredentialKind.CODE}


# --- Admin side ----------------------------------------------------------------------------


def session_password(db: Session, keyring: Keyring, owner: User, public_id: str) -> str:
    """The current session password, for display in the console and on the screen (FR-20)."""
    return keyring.decrypt(get_assembly(db, owner, public_id).access_password_enc)


def set_session_password(
    db: Session,
    keyring: Keyring,
    owner: User,
    public_id: str,
    raw: str | None = None,
    *,
    now: dt.datetime | None = None,
) -> str:
    """Replace the session password with ``raw``, or with a new random one when omitted.

    Voters who already joined stay connected (FR-20).
    """
    assembly = get_assembly(db, owner, public_id)
    require_status(assembly, AssemblyStatus.PREPARATION, AssemblyStatus.IN_SESSION)
    if raw is None:
        password, action = new_session_password(), "access.password_regenerated"
    else:
        password, action = " ".join(raw.split()), "access.password_changed"
        length = len(normalize_secret(password))
        if length < MIN_SESSION_PASSWORD_LENGTH:
            raise InvalidInputError("password_too_short")
        if length > MAX_SESSION_PASSWORD_LENGTH:
            raise InvalidInputError("password_too_long")
    assembly.access_password_enc = keyring.encrypt(password)
    audit.record(db, action, user_id=owner.id, assembly_id=assembly.id, now=now)
    return password


def _require_no_open_poll(db: Session, assembly: Assembly, code: str) -> None:
    """Refuse a change that hands out new voter credentials while a poll accepts ballots.

    Someone who already voted on that poll could otherwise vote on it again with the new
    credential: a device token after switching to codes mode, a fresh code after switching
    back or after a lost batch is replaced. Credentials carry no link to ballots, so a vote
    already cast cannot be found and withdrawn.
    """
    open_polls = db.scalar(
        select(func.count())
        .select_from(Poll)
        .where(Poll.assembly_id == assembly.id, Poll.status == PollStatus.OPEN)
    )
    if open_polls:
        raise InvalidStateError(code)


def set_access_mode(
    db: Session, owner: User, public_id: str, mode: AccessMode, *, now: dt.datetime | None = None
) -> Assembly:
    """Switch between password and codes mode; refused while a poll is open."""
    assembly = get_assembly(db, owner, public_id)
    require_status(assembly, AssemblyStatus.PREPARATION, AssemblyStatus.IN_SESSION)
    if assembly.access_mode is not mode:
        _require_no_open_poll(db, assembly, "access_mode_poll_open")
        assembly.access_mode = mode
        audit.record(
            db,
            "access.mode_changed",
            user_id=owner.id,
            assembly_id=assembly.id,
            details={"mode": mode.value},
            now=now,
        )
    return assembly


def set_entries_locked(
    db: Session, owner: User, public_id: str, locked: bool, *, now: dt.datetime | None = None
) -> Assembly:
    """Lock or unlock entries (FR-27). Voters who already joined are not affected."""
    assembly = get_assembly(db, owner, public_id)
    require_status(assembly, AssemblyStatus.PREPARATION, AssemblyStatus.IN_SESSION)
    if assembly.entries_locked != locked:
        assembly.entries_locked = locked
        action = "access.entries_locked" if locked else "access.entries_unlocked"
        audit.record(db, action, user_id=owner.id, assembly_id=assembly.id, now=now)
    return assembly


def set_headcount(
    db: Session,
    owner: User,
    public_id: str,
    headcount: int | None,
    *,
    now: dt.datetime | None = None,
) -> Assembly:
    """Number of people counted in the room, for the headcount check (FR-28).

    The audit entry is also an attendance reading (``services/attendance.py``): it keeps the
    connections at that moment next to the headcount. Never blocks anything.

    At most the account's codes quota, the cap of a session without a member count: the
    headcount raises the access cap (``access_cap``), and a delegate link holding only this
    right must not lift it without bound.
    """
    if headcount is not None and headcount < 0:
        raise InvalidInputError("invalid_headcount")
    if headcount is not None and headcount > quotas(owner).codes_per_assembly:
        raise InvalidInputError("headcount_too_large")
    assembly = get_assembly(db, owner, public_id)
    assembly.headcount = headcount
    audit.record(
        db,
        "access.headcount_set",
        user_id=owner.id,
        assembly_id=assembly.id,
        details={"headcount": headcount, "attendees": attendees(db, assembly)},
        now=now,
    )
    return assembly


def screen_token(db: Session, keyring: Keyring, owner: User, public_id: str) -> str:
    return keyring.decrypt(get_assembly(db, owner, public_id).screen_token_enc)


def regenerate_screen_token(
    db: Session, keyring: Keyring, owner: User, public_id: str, *, now: dt.datetime | None = None
) -> str:
    """New projection screen link; the previous one stops working (FR-16)."""
    assembly = get_assembly(db, owner, public_id)
    token = new_token()
    assembly.screen_token_enc = keyring.encrypt(token)
    audit.record(
        db, "access.screen_link_regenerated", user_id=owner.id, assembly_id=assembly.id, now=now
    )
    return token


def public_results_token(db: Session, keyring: Keyring, owner: User, public_id: str) -> str | None:
    encrypted = get_assembly(db, owner, public_id).public_token_enc
    return None if encrypted is None else keyring.decrypt(encrypted)


def set_public_results(
    db: Session,
    keyring: Keyring,
    owner: User,
    public_id: str,
    enabled: bool,
    *,
    now: dt.datetime | None = None,
) -> str | None:
    """Enable (with a new secret link) or disable the public results link (FR-17)."""
    assembly = get_assembly(db, owner, public_id)
    token = new_token() if enabled else None
    assembly.public_token_enc = None if token is None else keyring.encrypt(token)
    action = "access.public_link_enabled" if enabled else "access.public_link_disabled"
    audit.record(db, action, user_id=owner.id, assembly_id=assembly.id, now=now)
    return token


def check_link_token(
    db: Session, keyring: Keyring, public_id: str, token: str, *, public: bool = False
) -> Assembly:
    """Session behind a projection screen link, or a public results link when ``public``."""
    assembly = find_public_assembly(db, public_id)
    encrypted = None
    if assembly is not None:
        encrypted = assembly.public_token_enc if public else assembly.screen_token_enc
    if (
        assembly is None
        or encrypted is None
        or not secrets_match(keyring.decrypt(encrypted), token)
    ):
        raise NotFoundError("link")
    return assembly


@dataclass(frozen=True, slots=True)
class CodeBatchCreated:
    batch: CodeBatch
    codes: list[str]  # display form, shown once for the card PDF and never stored


def create_code_batch(
    db: Session,
    keyring: Keyring,
    owner: User,
    public_id: str,
    size: int,
    *,
    now: dt.datetime | None = None,
) -> CodeBatchCreated:
    """Generate ``size`` individual codes (FR-23); only their HMAC digests are stored.
    Refused while a poll is open (a replaced batch would let its holders vote twice)."""
    assembly = get_assembly(db, owner, public_id)
    require_status(assembly, AssemblyStatus.PREPARATION, AssemblyStatus.IN_SESSION)
    _require_no_open_poll(db, assembly, "codes_poll_open")
    if not 1 <= size <= MAX_CODES_PER_BATCH:
        raise InvalidInputError("invalid_batch_size")
    # Revoked batches count too: otherwise generating and revoking in a loop would store codes,
    # and render card PDFs, without bound. The quota stays far above a room's needs, so a lost
    # PDF can still be replaced several times.
    generated = db.scalar(
        select(func.coalesce(func.sum(CodeBatch.size), 0)).where(
            CodeBatch.assembly_id == assembly.id
        )
    )
    if (generated or 0) + size > quotas(assembly.owner).codes_per_assembly:
        raise QuotaExceededError("codes_per_assembly")

    batch = CodeBatch(assembly_id=assembly.id, size=size, created_at=now)
    db.add(batch)
    db.flush()
    taken = set(
        db.scalars(
            select(VoterCredential.code_hmac).where(VoterCredential.assembly_id == assembly.id)
        )
    )
    codes: list[str] = []
    credentials: list[VoterCredential] = []
    while len(codes) < size:
        code = new_voter_code()
        digest = keyring.code_digest(code)
        if digest in taken:
            continue
        taken.add(digest)
        codes.append(format_voter_code(code))
        credentials.append(
            VoterCredential(
                assembly_id=assembly.id,
                kind=CredentialKind.CODE,
                code_hmac=digest,
                batch_id=batch.id,
            )
        )
    # Insert in an order unrelated to the cards of the PDF: otherwise credential ids, and the
    # physical order of rows, would tell which card a participation belongs to (SPEC §9.2).
    secrets.SystemRandom().shuffle(credentials)
    db.add_all(credentials)
    db.flush()
    audit.record(
        db,
        "codes.batch_created",
        user_id=owner.id,
        assembly_id=assembly.id,
        details={"batch_id": batch.id, "size": size},
        now=now,
    )
    return CodeBatchCreated(batch=batch, codes=codes)


def revoke_code_batch(
    db: Session, owner: User, public_id: str, batch_id: int, *, now: dt.datetime | None = None
) -> CodeBatch:
    """Revoke a batch (lost PDF): its codes stop working, including those already in use."""
    assembly = get_assembly(db, owner, public_id)
    batch = db.scalar(
        select(CodeBatch).where(CodeBatch.id == batch_id, CodeBatch.assembly_id == assembly.id)
    )
    if batch is None:
        raise NotFoundError("code_batch")
    if batch.status is not BatchStatus.REVOKED:
        batch.status = BatchStatus.REVOKED
        audit.record(
            db,
            "codes.batch_revoked",
            user_id=owner.id,
            assembly_id=assembly.id,
            details={"batch_id": batch.id},
            now=now,
        )
    return batch


def list_code_batches(db: Session, owner: User, public_id: str) -> list[CodeBatch]:
    assembly = get_assembly(db, owner, public_id)
    return list(
        db.scalars(
            select(CodeBatch).where(CodeBatch.assembly_id == assembly.id).order_by(CodeBatch.id)
        )
    )


def _usable(assembly: Assembly) -> ColumnElement[bool]:
    """Credentials of the current access mode that may vote."""
    kind = _KIND_FOR_MODE[assembly.access_mode]
    active_batch = (
        select(CodeBatch.id)
        .where(CodeBatch.assembly_id == assembly.id, CodeBatch.status == BatchStatus.ACTIVE)
        .scalar_subquery()
    )
    return and_(
        VoterCredential.assembly_id == assembly.id,
        VoterCredential.kind == kind,
        VoterCredential.activated.is_(True),
        or_(VoterCredential.batch_id.is_(None), VoterCredential.batch_id.in_(active_batch)),
    )


def attendees(db: Session, assembly: Assembly) -> int:
    """Credentials created (password mode) or codes activated (codes mode) (FR-26)."""
    count = db.scalar(select(func.count()).select_from(VoterCredential).where(_usable(assembly)))
    return count or 0


def _demo_entries(db: Session, assembly: Assembly) -> int:
    """Every credential ever activated in a session, whatever its access mode or batch: the
    limit of a demo session (``DEMO_VOTERS``). Counting only the usable ones, a revoked batch
    would let 5 more voters in during the same poll."""
    count = db.scalar(
        select(func.count())
        .select_from(VoterCredential)
        .where(VoterCredential.assembly_id == assembly.id, VoterCredential.activated.is_(True))
    )
    return count or 0


def _with_margin(people: int) -> int:
    return people + max(ACCESS_CAP_MIN_MARGIN, math.ceil(people * ACCESS_CAP_MARGIN))


def access_cap(assembly: Assembly) -> int:
    """Most device credentials a session issues in password mode (FR-27).

    The registered members plus a margin (10 %, at least 10) for those who change device;
    without a member count, the account's codes quota, the same ceiling as one code per person.
    Either way a flood of joins with the projected password stays bounded. The headcount
    (FR-28) raises the cap, never lowers it: the member count is frozen once the session is
    open, so it is the tribune's way to let a larger room in. A demo session: ``DEMO_VOTERS``.
    """
    if assembly.is_demo:
        return DEMO_VOTERS
    if assembly.registered_count is None:
        cap = quotas(assembly.owner).codes_per_assembly
    else:
        cap = _with_margin(assembly.registered_count)
    if assembly.headcount is not None:
        cap = max(cap, _with_margin(assembly.headcount))
    return cap


def access_cap_reached(assembly: Assembly, present: int) -> int | None:
    """The access cap once ``present`` credentials reach it in password mode (or in a demo
    session, in either mode), else None."""
    if assembly.access_mode is not AccessMode.PASSWORD and not assembly.is_demo:
        return None
    cap = access_cap(assembly)
    return cap if present >= cap else None


@dataclass(frozen=True, slots=True)
class HeadcountWarning:
    kind: str  # "attendees" or "ballots"
    count: int
    headcount: int
    poll_id: int | None = None


def headcount_warnings(db: Session, assembly: Assembly) -> list[HeadcountWarning]:
    """Credentials in use, or ballots on a poll, above the headcount (FR-28)."""
    headcount = assembly.headcount
    if headcount is None:
        return []
    warnings: list[HeadcountWarning] = []
    present = attendees(db, assembly)
    if present > headcount:
        warnings.append(HeadcountWarning("attendees", present, headcount))
    rows = db.execute(
        select(Poll.id, func.count(Participation.credential_id))
        .join(Participation, Participation.poll_id == Poll.id)
        .where(
            Poll.assembly_id == assembly.id,
            Poll.status.in_((PollStatus.OPEN, PollStatus.CLOSED, PollStatus.PUBLISHED)),
        )
        .group_by(Poll.id)
        .order_by(Poll.id)
    )
    for poll_id, ballots in rows:
        if ballots > headcount:
            warnings.append(HeadcountWarning("ballots", ballots, headcount, poll_id))
    return warnings


# --- Voter side ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Joined:
    credential: VoterCredential
    token: str  # goes into the device cookie; only its hash is stored


def _open_assembly(db: Session, public_id: str) -> Assembly:
    assembly = find_public_assembly(db, public_id)
    if assembly is None:
        raise NotFoundError("assembly")
    if assembly.status is not AssemblyStatus.IN_SESSION:
        raise AccessDeniedError("session_not_in_session")
    return assembly


def join(db: Session, keyring: Keyring, public_id: str, secret: str) -> Joined:
    """Check a session password or an individual code and issue a device credential.

    Password mode refuses a new device once the access cap is reached (``access_cap``), checked
    after the password so that only someone who holds it learns that the session is full. A
    demo session (FR-09a) also refuses a code not yet in use past ``DEMO_VOTERS``.
    """
    assembly = _open_assembly(db, public_id)
    typed = normalize_secret(secret)
    token = new_token()

    if assembly.access_mode is AccessMode.PASSWORD:
        if assembly.entries_locked:
            raise AccessDeniedError("entries_locked")
        expected = normalize_secret(keyring.decrypt(assembly.access_password_enc))
        if not secrets_match(typed, expected):
            raise AccessDeniedError("wrong_secret")
        # Counted in the caller's write transaction (BEGIN IMMEDIATE): concurrent joins wait
        # for each other, so they never overshoot the cap.
        present = _demo_entries(db, assembly) if assembly.is_demo else attendees(db, assembly)
        if access_cap_reached(assembly, present) is not None:
            raise AccessDeniedError(DEMO_VOTERS_FULL if assembly.is_demo else ACCESS_CAP_REACHED)
        credential = VoterCredential(
            assembly_id=assembly.id,
            kind=CredentialKind.DEVICE,
            token_hash=hash_token(token),
            activated=True,
        )
        db.add(credential)
        db.flush()
        return Joined(credential=credential, token=token)

    credential = None
    if len(typed) == VOTER_CODE_LENGTH:
        credential = db.scalar(
            select(VoterCredential).where(
                _usable_or_dormant(assembly),
                VoterCredential.code_hmac == keyring.code_digest(typed),
            )
        )
    if credential is None:
        raise AccessDeniedError("wrong_secret")
    if not credential.activated:
        # A code already in use may be entered again on another device, even when locked.
        if assembly.entries_locked:
            raise AccessDeniedError("entries_locked")
        if assembly.is_demo and _demo_entries(db, assembly) >= DEMO_VOTERS:
            raise AccessDeniedError(DEMO_VOTERS_FULL)
        credential.activated = True
    credential.token_hash = hash_token(token)  # the previous device, if any, loses access
    db.flush()
    return Joined(credential=credential, token=token)


def _usable_or_dormant(assembly: Assembly) -> ColumnElement[bool]:
    """Codes of active batches, activated or not yet."""
    return and_(
        VoterCredential.assembly_id == assembly.id,
        VoterCredential.kind == CredentialKind.CODE,
        VoterCredential.batch_id.in_(
            select(CodeBatch.id).where(
                CodeBatch.assembly_id == assembly.id, CodeBatch.status == BatchStatus.ACTIVE
            )
        ),
    )


def credential_for(db: Session, public_id: str, token: str | None) -> VoterCredential | None:
    """The valid credential behind a device cookie, or None."""
    if not token:
        return None
    assembly = find_public_assembly(db, public_id)
    if assembly is None or assembly.status is not AssemblyStatus.IN_SESSION:
        return None
    return db.scalar(
        select(VoterCredential).where(
            _usable(assembly), VoterCredential.token_hash == hash_token(token)
        )
    )


def former_credential(db: Session, public_id: str, token: str | None) -> VoterCredential | None:
    """The credential a device held in a session now closed: it votes no more, but the device
    still reads the results published to voters (FR-36). None for any other device, one that
    left ("Quitter"), or a session not closed."""
    if not token:
        return None
    assembly = find_public_assembly(db, public_id)
    if assembly is None or assembly.status is not AssemblyStatus.CLOSED:
        return None
    return db.scalar(
        select(VoterCredential).where(
            VoterCredential.assembly_id == assembly.id,
            VoterCredential.token_hash == hash_token(token),
        )
    )


def leave(db: Session, public_id: str, token: str | None) -> None:
    """End the session on this device ("Terminer", FR-46); a code can be entered again."""
    if not token:
        return
    assembly = find_public_assembly(db, public_id)
    if assembly is None:
        return
    credential = db.scalar(
        select(VoterCredential).where(
            VoterCredential.assembly_id == assembly.id,
            VoterCredential.token_hash == hash_token(token),
        )
    )
    if credential is not None:
        credential.token_hash = None
        db.flush()
