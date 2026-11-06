"""Demo accounts (FR-09a): anyone tries the console with an account of their own, and nothing is
kept.

``start`` creates the account (no password, an address in ``.invalid`` that never receives
mail) with a session in preparation and a closed session already voted on. Its login lasts 2 h
without activity and 24 h at most (``auth.DEMO_SESSION_LIFETIME``); ``purge`` then deletes the
account with its sessions, even one in session, and so does ``end`` when the visitor leaves.

The example content is user interface text: written in English, translated in the catalogs.
"""

from __future__ import annotations

import random
import secrets
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from agaura.db import cancel_compaction
from agaura.i18n import _
from agaura.models import Assembly, Poll, PollStatus, User, UserSession, utcnow
from agaura.security import Keyring
from agaura.services import access, accounts, assemblies, audit, ballots, delegates, meeting, polls
from agaura.services.assemblies import AssemblyInput
from agaura.services.audit import Actor
from agaura.services.auth import (
    DEMO_RENEW_WHEN_LEFT,
    DEMO_SESSION_LIFETIME,
    DEMO_SESSION_MAX_AGE,
)
from agaura.services.errors import InvalidStateError
from agaura.services.polls import PollInput

EMAIL_DOMAIN = "demo.invalid"  # reserved (RFC 2606): no mail can ever reach it
PAST_VOTERS = 24  # simulated voters of last year's session (local accounts of dev_seed.py)
Ballot = Mapping[str, Any] | None
# A full pool frees the demo idle the longest, once idle this long (FR-09a): a few addresses
# opening demos and leaving them would otherwise keep every place for 2 hours.
DEMO_IDLE_BEFORE_FREED = timedelta(minutes=30)


def _tribune() -> list[meeting.Member]:
    return [
        meeting.Member("Marie Dupont", _("Chair of the session")),
        meeting.Member("Paul Martin", _("Secretary")),
        meeting.Member("Ali Benali", _("Teller")),
    ]


def _agenda(year: int) -> list[str]:
    return [
        _("Welcome and quorum check"),
        _("Activity report"),
        _("Financial report %(year)s") % {"year": year - 1},
        _("Election to the board"),
        _("Projects to fund"),
        _("Venue of the next assembly"),
        _("Any other business"),
    ]


def _venues() -> list[str]:
    return [_("Village hall"), _("Community centre"), _("Media library")]


def _projects() -> list[str]:
    return [_("Shared garden"), _("Mural"), _("Bike workshop")]


def _names() -> list[str]:
    return [_("The Gazette"), _("Neighbourhood echo"), _("The Link")]


CANDIDATES = ["Alice Martin", "Bruno Petit", "Chloé Durand", "David Leroy"]


def _drafts() -> list[PollInput]:
    return [
        PollInput(
            title=_("Approval of the activity report"),
            method="single_choice",
            options=[_("For"), _("Against")],
            params={"adopt_option": 1},
            blank_label=_("Abstention"),
        ),
        PollInput(title=_("Venue of the next assembly"), method="single_choice", options=_venues()),
        PollInput(
            title=_("Election to the board (2 seats)"),
            method="approval",
            options=CANDIDATES,
            params={"seats": 2, "max_choices": 2},
        ),
        PollInput(title=_("Projects to fund"), method="majority_judgment", options=_projects()),
        PollInput(title=_("Name of the newsletter"), method="condorcet", options=_names()),
    ]


def _approve(rng: random.Random) -> Ballot:
    return rng.choices([{"choice": 1}, {"choice": 2}, None], weights=[70, 20, 10])[0]


def _choose_venue(rng: random.Random) -> Ballot:
    return {"choice": rng.choices([1, 2, 3], weights=[45, 35, 20])[0]}


def _elect(rng: random.Random) -> Ballot:
    popularity = {1: 0.8, 2: 0.6, 3: 0.55, 4: 0.3}
    approved = [option for option, p in popularity.items() if rng.random() < p][:2]
    return {"approved": approved} if approved else None


def _grade(rng: random.Random) -> Ballot:
    centres = {1: 1.2, 2: 2.0, 3: 3.5}
    return {
        "grades": {
            option: min(5, max(0, round(rng.gauss(centre, 1.1))))
            for option, centre in centres.items()
        }
    }


def _rank(rng: random.Random) -> Ballot:
    order = rng.choices([(1, 2, 3), (2, 3, 1), (3, 1, 2), (1, 3, 2)], weights=[35, 30, 20, 15])[0]
    return {"ranks": {option: place for place, option in enumerate(order, start=1)}}


def _past_polls(year: int) -> list[tuple[PollInput, Callable[[random.Random], Ballot]]]:
    """The polls of last year's session and how its simulated voters answer them."""
    return [
        (
            PollInput(
                title=_("Approval of the %(year)s accounts") % {"year": year - 1},
                method="single_choice",
                options=[_("For"), _("Against")],
                params={"adopt_option": 1},
                blank_label=_("Abstention"),
            ),
            _approve,
        ),
        (
            PollInput(
                title=_("Venue of the next assembly"), method="single_choice", options=_venues()
            ),
            _choose_venue,
        ),
        (
            PollInput(
                title=_("Election to the board (2 seats)"),
                method="approval",
                options=CANDIDATES,
                params={"seats": 2, "max_choices": 2},
            ),
            _elect,
        ),
        (
            PollInput(title=_("Projects to fund"), method="majority_judgment", options=_projects()),
            _grade,
        ),
        (
            PollInput(title=_("Name of the newsletter"), method="condorcet", options=_names()),
            _rank,
        ),
    ]


def fill(db: Session, keyring: Keyring, owner: User, *, now: datetime | None = None) -> None:
    """Give ``owner`` the example sessions: last year's, closed, and one in preparation."""
    now = now or utcnow()
    past_session(db, keyring, owner, now=now)
    preparation_session(db, keyring, owner, now=now)


def preparation_session(
    db: Session, keyring: Keyring, owner: User, *, now: datetime | None = None
) -> tuple[str, str]:
    """A session in preparation with five drafts, a tribune, an agenda and a delegate link;
    returns its ID and the key of the link. Also used by ``scripts/dev_seed.py``."""
    now = now or utcnow()
    assembly = assemblies.create_assembly(
        db,
        keyring,
        owner,
        AssemblyInput(
            title=_("Demonstration general assembly"),
            place=_("Village hall"),
            registered_count=20,  # quorum 1/4: 5, what a demo session can reach
            quorum_rule="1/4",
        ),
        now=now,
    )
    public_id = assembly.public_id
    for data in _drafts():
        polls.create_poll(db, owner, public_id, data, now=now)
    meeting.set_tribune(db, owner, public_id, _tribune())
    meeting.set_agenda(db, owner, public_id, _agenda(now.year))
    _delegate, key = delegates.create_delegate(
        db, keyring, owner, public_id, _("Paul Martin, secretary"), ["notes", "timer"], now=now
    )
    return public_id, key


def past_session(db: Session, keyring: Keyring, owner: User, *, now: datetime | None = None) -> str:
    """Last year's session, closed, its polls voted on by simulated voters and published;
    returns its ID. Also used by ``scripts/dev_seed.py``."""
    now = now or utcnow()
    rng = random.Random(2025)  # noqa: S311 (simulated votes, the same in every demo)
    past = _past_polls(now.year)
    assembly = assemblies.create_assembly(
        db,
        keyring,
        owner,
        AssemblyInput(
            title=_("General assembly %(year)s") % {"year": now.year - 1},
            date=now.date().replace(year=now.year - 1, day=min(now.day, 28)),
            place=_("Community centre"),
            registered_count=20,  # quorum 1/4: 5, what a demo session can reach
            quorum_rule="1/4",
        ),
        now=now,
    )
    public_id = assembly.public_id
    poll_ids = [polls.create_poll(db, owner, public_id, data, now=now).id for data, _ in past]
    meeting.set_tribune(db, owner, public_id, _tribune())
    assemblies.open_assembly(db, owner, public_id, now=now)
    password = access.session_password(db, keyring, owner, public_id)
    # A demo session takes 5 voters at most, its example too.
    voters = access.DEMO_VOTERS if owner.is_demo else PAST_VOTERS
    tokens = [access.join(db, keyring, public_id, password).token for _ in range(voters)]
    for poll_id, (_data, make_ballot) in zip(poll_ids, past, strict=True):
        polls.open_poll(db, owner, poll_id, now=now)
        for token in tokens:
            ballots.cast_vote(db, public_id, token, poll_id, make_ballot(rng))
        polls.close_poll(db, owner, poll_id, now=now)
        polls.publish_result(db, owner, poll_id, voters=True, now=now)
    assemblies.close_assembly(db, owner, public_id, now=now)
    return public_id


def count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.is_demo.is_(True))) or 0


def start(db: Session, keyring: Keyring, *, limit: int, now: datetime | None = None) -> User:
    """A new demo account holding the example sessions; ``demo_full`` past ``limit`` accounts
    at once. The caller logs it in (``auth.start_session``)."""
    now = now or utcnow()
    if count(db) >= limit:
        raise InvalidStateError("demo_full")
    user = accounts._new_user(
        db,
        f"demo-{secrets.token_hex(8)}@{EMAIL_DOMAIN}",
        None,
        display_name=None,
        verified=True,  # nothing to confirm: it never receives mail
        superadmin=False,
        now=now,
        demo=True,
    )
    audit.record(db, "account.demo_started", actor=Actor.SYSTEM, user_id=user.id, now=now)
    fill(db, keyring, user, now=now)
    # Closing the example polls asks for a rebuild of the database file, which would hold every
    # vote of the platform back for its length: their ballots are simulated, nothing to hide.
    cancel_compaction(db)
    return user


def free_a_place(db: Session, *, limit: int, now: datetime | None = None) -> list[str]:
    """With ``limit`` demo accounts open, delete the one idle the longest if no request reached
    it for ``DEMO_IDLE_BEFORE_FREED`` (or in the last minutes of its 24 hours); returns the IDs
    of its sessions, so that their voters, screens and delegates learn it. Nothing otherwise.

    No column records each request: a login's end moves to 2 hours after the last request, at
    most every 15 minutes (``auth.DEMO_RENEW_WHEN_LEFT``), so an end closer than 2 hours minus
    those 15 minutes and the idle time means at least that long without a request.
    """
    now = now or utcnow()
    if count(db) < limit:
        return []
    renewal_step = DEMO_SESSION_LIFETIME - DEMO_RENEW_WHEN_LEFT
    latest_end = now + DEMO_SESSION_LIFETIME - renewal_step - DEMO_IDLE_BEFORE_FREED
    login_end = func.coalesce(
        select(func.max(UserSession.expires_at))
        .where(UserSession.user_id == User.id)
        .correlate(User)
        .scalar_subquery(),
        User.created_at,  # never logged in, or logged out: idle already
    )
    idle = db.scalars(
        select(User)
        .where(User.is_demo.is_(True), login_end <= latest_end)
        .order_by(login_end, User.id)
        .limit(1)
    ).first()
    if idle is None:
        return []
    public_ids = _public_ids(db, idle)
    accounts._delete_with_sessions(
        db, idle, actor=Actor.SYSTEM, details={}, now=now, in_session_too=True
    )
    _defer_compaction_during_a_vote(db)
    return public_ids


def _defer_compaction_during_a_vote(db: Session) -> None:
    """A deletion asks for a rebuild of the database file (``accounts._delete_with_sessions``),
    which holds every vote back for its length. While a poll of a real session is open, the
    rebuild waits for its closing, which rebuilds the file anyway; the deleted rows are gone
    already (``secure_delete``)."""
    real_vote = (
        select(Poll.id)
        .join(Assembly, Assembly.id == Poll.assembly_id)
        .where(Poll.status == PollStatus.OPEN, Assembly.is_demo.is_(False))
    )
    if db.scalar(real_vote.limit(1)) is not None:
        cancel_compaction(db)


def _public_ids(db: Session, user: User) -> list[str]:
    return list(db.scalars(select(Assembly.public_id).where(Assembly.owner_id == user.id)))


def end(db: Session, user: User, *, now: datetime | None = None) -> list[str]:
    """Delete a demo account and its sessions at the visitor's request; returns their IDs, so
    that their voters, screens and delegates learn it. Nothing for another account."""
    account = db.get(User, user.id)
    if account is None or not account.is_demo:
        return []
    public_ids = _public_ids(db, account)
    accounts._delete_with_sessions(
        db, account, actor=Actor.USER, details={}, now=now, in_session_too=True
    )
    _defer_compaction_during_a_vote(db)
    return public_ids


def purge(db: Session, *, now: datetime | None = None) -> list[str]:
    """Delete the demo accounts whose login has ended (2 h without activity, 24 h at most, or
    logged out); returns the IDs of the sessions that went with them."""
    now = now or utcnow()
    live_login = exists().where(UserSession.user_id == User.id, UserSession.expires_at > now)
    expired = db.scalars(
        select(User).where(
            User.is_demo.is_(True),
            or_(~live_login, User.created_at <= now - DEMO_SESSION_MAX_AGE),
        )
    ).all()
    public_ids: list[str] = []
    for account in expired:
        public_ids += _public_ids(db, account)
        accounts._delete_with_sessions(
            db, account, actor=Actor.SYSTEM, details={}, now=now, in_session_too=True
        )
    if expired:
        _defer_compaction_during_a_vote(db)
    return public_ids
