"""Demo accounts (FR-09a): example content, lifetime of the login, purge, and what they never
reach (email, super-admin page, deployment and backup guards)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from agaura.db import Database
from agaura.models import (
    AccessMode,
    Assembly,
    AssemblyStatus,
    CodeBatch,
    Delegate,
    OutboxEmail,
    PollStatus,
    User,
    UserSession,
    utcnow,
)
from agaura.security import Keyring
from agaura.services import access, accounts, assemblies, auth, demo
from agaura.services.errors import (
    AccessDeniedError,
    InvalidInputError,
    InvalidStateError,
    NotFoundError,
    QuotaExceededError,
)
from agaura.services.polls import PollInput
from tests.conftest import World


def open_demo(database: Database, keyring: Keyring, **kwargs: object) -> tuple[User, str]:
    with database.write() as db:
        user = demo.start(db, keyring, limit=50, **kwargs)  # type: ignore[arg-type]
        return user, auth.start_session(db, user, now=kwargs.get("now"))  # type: ignore[arg-type]


def sessions_of(database: Database, user: User) -> list[Assembly]:
    with database.read() as db:
        return list(
            db.scalars(select(Assembly).where(Assembly.owner_id == user.id).order_by(Assembly.id))
        )


def question(title: str) -> PollInput:
    return PollInput(title=title, method="single_choice", options=["Oui", "Non"])


def accounts_left(database: Database) -> int:
    with database.read() as db:
        return db.scalar(select(func.count()).select_from(User)) or 0


def test_a_demo_holds_the_example_sessions(database: Database, keyring: Keyring) -> None:
    user, _token = open_demo(database, keyring)
    assert user.is_demo and user.password_hash is None and user.email_verified_at is not None
    assert user.email.endswith("@demo.invalid")
    past, current = sessions_of(database, user)
    assert all(assembly.is_demo for assembly in (past, current))
    assert past.status is AssemblyStatus.CLOSED
    assert current.status is AssemblyStatus.PREPARATION
    assert current.title == "Assemblée générale de démonstration"  # through the catalogs
    with database.read() as db:
        past = db.get_one(Assembly, past.id)
        assert len(past.polls) == 5
        assert all(poll.result is not None and poll.result.published_at for poll in past.polls)
        assert all(poll.result.data["counts"]["cast"] == access.DEMO_VOTERS for poll in past.polls)
        assert past.registered_count == 20 and assemblies.quorum_required(past) == 5
        current = db.get_one(Assembly, current.id)
        assert [poll.status for poll in current.polls] == [PollStatus.DRAFT] * 5
        assert len(current.tribune) == 3 and len(current.agenda) == 7
        assert db.scalar(select(func.count()).where(Delegate.assembly_id == current.id)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEmail)) == 0  # no email


def test_each_visitor_gets_an_account_of_their_own(database: Database, keyring: Keyring) -> None:
    first, _ = open_demo(database, keyring)
    second, _ = open_demo(database, keyring)
    assert first.id != second.id and first.email != second.email


def test_demos_at_once_are_capped(database: Database, keyring: Keyring) -> None:
    with database.write() as db:
        demo.start(db, keyring, limit=1)
    with pytest.raises(InvalidStateError, match="demo_full"), database.write() as db:
        demo.start(db, keyring, limit=1)


def test_a_full_pool_frees_the_demo_idle_the_longest(database: Database, keyring: Keyring) -> None:
    """Idle 30 minutes at least: a login's end moves at most every 15 minutes, so its end
    tells the last request within that step (``free_a_place``)."""
    start = utcnow()
    oldest, _ = open_demo(database, keyring, now=start)
    recent, recent_token = open_demo(database, keyring, now=start + timedelta(minutes=10))
    soon = start + timedelta(minutes=40)  # oldest idle 40 min, but the step may hide 15
    with database.write() as db:
        assert demo.free_a_place(db, limit=2, now=soon) == []
    later = start + timedelta(minutes=46)
    with database.write() as db:
        auth.touch(db, recent_token, now=later)  # the other visitor is still there
    with database.write() as db:
        assert demo.free_a_place(db, limit=3, now=later) == []  # a place is free: nothing
        freed = demo.free_a_place(db, limit=2, now=later)
    assert len(freed) == 2  # the two example sessions of the oldest
    with database.read() as db:
        assert db.get(User, oldest.id) is None
        assert db.get(User, recent.id) is not None


def test_a_full_pool_of_active_demos_stays_full(database: Database, keyring: Keyring) -> None:
    start = utcnow()
    _first, first_token = open_demo(database, keyring, now=start)
    open_demo(database, keyring, now=start)
    with database.write() as db:
        auth.touch(db, first_token, now=start + timedelta(minutes=50))
    with database.write() as db:  # the second, idle 50 minutes, goes; never the first
        assert len(demo.free_a_place(db, limit=2, now=start + timedelta(minutes=50))) == 2
    with database.write() as db:
        assert demo.free_a_place(db, limit=1, now=start + timedelta(minutes=55)) == []
        with pytest.raises(InvalidStateError, match="demo_full"):
            demo.start(db, keyring, limit=1)


def test_demo_quotas_are_lower(world: World, database: Database, keyring: Keyring) -> None:
    user, _ = open_demo(database, keyring)
    assert accounts.quotas(user) == accounts.DEMO_QUOTAS
    world.assembly(user, in_session=False)  # 2 open: the example in preparation and this one
    world.assembly(user, in_session=False)
    with pytest.raises(QuotaExceededError):
        world.assembly(user, in_session=False)
    public_id = sessions_of(database, user)[1].public_id
    for number in range(accounts.DEMO_QUOTAS.polls_per_assembly - 5):
        world.poll(user, public_id, question(f"Q{number}"), open_it=False)
    with pytest.raises(QuotaExceededError):
        world.poll(user, public_id, question("One too many"), open_it=False)


def test_the_login_lasts_two_hours_idle_and_a_day_at_most(
    database: Database, keyring: Keyring
) -> None:
    start = utcnow()
    _user, token = open_demo(database, keyring, now=start)
    with database.read() as db:
        assert auth.lookup(db, token, now=start + timedelta(hours=1, minutes=59)) is not None
        assert auth.lookup(db, token, now=start + timedelta(hours=2, minutes=1)) is None
    # Active all along: renewed, never beyond 24 h after opening.
    moment = start
    while moment + timedelta(minutes=50) < start + auth.DEMO_SESSION_MAX_AGE:
        moment += timedelta(minutes=50)
        with database.read() as db:
            found = auth.lookup(db, token, now=moment)
        assert found is not None, moment - start
        if found.needs_touch:
            with database.write() as db:
                auth.touch(db, token, now=moment)
    with database.read() as db:
        assert auth.lookup(db, token, now=start + timedelta(hours=24, minutes=1)) is None


def test_purge_deletes_ended_demos_only(world: World, database: Database, keyring: Keyring) -> None:
    start = utcnow()
    idle, _ = open_demo(database, keyring, now=start)
    active, active_token = open_demo(database, keyring, now=start)
    real = world.user("real@example.org")
    world.assembly(real)
    with database.write() as db:  # a real account whose login ended long ago is never purged
        auth.start_session(db, real, now=start - timedelta(days=30))
    later = start + timedelta(hours=1, minutes=50)
    with database.write() as db:
        auth.touch(db, active_token, now=later)
    with database.write() as db:
        assert demo.purge(db, now=later) == []
    gone = start + timedelta(hours=2, minutes=5)
    with database.write() as db:
        deleted = demo.purge(db, now=gone)
    assert len(deleted) == 2
    with database.read() as db:
        assert db.get(User, idle.id) is None
        assert db.get(User, active.id) is not None and db.get(User, real.id) is not None
    with database.write() as db:  # 24 h after opening, even while in use
        assert len(demo.purge(db, now=start + timedelta(hours=24))) == 2
    assert accounts_left(database) == 1


def test_purge_deletes_a_demo_whose_login_ended(database: Database, keyring: Keyring) -> None:
    _user, token = open_demo(database, keyring)
    with database.write() as db:
        auth.end_session(db, token)  # another account logged in on that browser
    with database.write() as db:
        assert len(demo.purge(db)) == 2
    assert accounts_left(database) == 0


def test_a_demo_in_session_is_deleted_all_the_same(
    world: World, database: Database, keyring: Keyring
) -> None:
    user, _ = open_demo(database, keyring)
    current = sessions_of(database, user)[1].public_id
    with database.write() as db:
        assemblies.open_assembly(db, user, current)
    token = world.voters(user, current, 1)[0]
    with database.write() as db:
        assert current in demo.end(db, user)
    with database.read() as db:
        assert db.scalar(select(func.count()).select_from(Assembly)) == 0
        assert db.scalar(select(func.count()).select_from(UserSession)) == 0
        assert access.credential_for(db, current, token) is None


def test_end_leaves_other_accounts_alone(world: World, database: Database) -> None:
    real = world.user("real@example.org")
    with database.write() as db:
        assert demo.end(db, real) == []
    assert accounts_left(database) == 1


def test_demos_stay_out_of_the_guards_and_the_superadmin_page(
    world: World, database: Database, keyring: Keyring
) -> None:
    user, _ = open_demo(database, keyring)
    current = sessions_of(database, user)[1].public_id
    with database.write() as db:
        assemblies.open_assembly(db, user, current)
        assert assemblies.count_in_session(db) == 0  # the deployment guard (/health)
    with database.write() as db:
        admin = accounts.create_user_by_operator(
            db, "admin@example.org", "admin password 2026", superadmin=True
        )
        rows = accounts.list_accounts(db, admin)
        assert [row.email for row in rows] == ["admin@example.org"]
    with pytest.raises(NotFoundError), database.write() as db:
        accounts.delete_account_by_superadmin(
            db, admin, user.id, user.email, password="admin password 2026"
        )


def test_a_demo_cannot_set_a_password_or_an_address(database: Database, keyring: Keyring) -> None:
    user, _ = open_demo(database, keyring)
    with pytest.raises(InvalidInputError, match="wrong_password"), database.write() as db:
        accounts.change_password(db, user, "", "a new password 2026")
    with pytest.raises(InvalidInputError, match="wrong_password"), database.write() as db:
        accounts.request_email_change(db, user, "", "me@example.org")
    with database.write() as db:
        assert accounts.request_verification(db, user) is None  # nothing to confirm


def count_compactions(monkeypatch: pytest.MonkeyPatch) -> list[Database]:
    calls: list[Database] = []
    monkeypatch.setattr(Database, "compact", lambda self: calls.append(self) or True)
    return calls


def test_opening_a_demo_never_rebuilds_the_database(
    database: Database, keyring: Keyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its example polls are closed on creation; their ballots are simulated."""
    calls = count_compactions(monkeypatch)
    open_demo(database, keyring)
    assert calls == []


def test_deleting_a_demo_rebuilds_the_database_unless_a_real_vote_is_open(
    world: World, database: Database, keyring: Keyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = count_compactions(monkeypatch)
    first, _ = open_demo(database, keyring)
    with database.write() as db:
        demo.end(db, first)
    assert len(calls) == 1
    real = world.user("real@example.org")
    world.poll(real, world.assembly(real))  # a real vote under way
    second, _ = open_demo(database, keyring)
    with database.write() as db:
        demo.end(db, second)
    with database.write() as db:
        demo.start(db, keyring, limit=50)  # never logged in: purged below
    with database.write() as db:
        assert len(demo.purge(db)) == 2
    assert len(calls) == 1  # the closing of the real poll rebuilds it


def opened_demo_session(
    database: Database, keyring: Keyring, **changes: object
) -> tuple[User, str]:
    """A demo account and its example session in preparation, opened."""
    user, _ = open_demo(database, keyring)
    public_id = sessions_of(database, user)[1].public_id
    with database.write() as db:
        assembly = db.scalars(select(Assembly).where(Assembly.public_id == public_id)).one()
        for name, value in changes.items():
            setattr(assembly, name, value)
        assemblies.open_assembly(db, user, public_id)
    return user, public_id


def test_a_demo_session_takes_five_voters_with_the_password(
    world: World, database: Database, keyring: Keyring
) -> None:
    user, public_id = opened_demo_session(database, keyring)
    world.voters(user, public_id, access.DEMO_VOTERS)
    with database.write() as db:  # the headcount never raises it
        access.set_headcount(db, user, public_id, 20)
    with pytest.raises(AccessDeniedError, match="demo_voters_full"):
        world.voters(user, public_id, 1)
    with database.read() as db:
        assembly = db.scalars(select(Assembly).where(Assembly.public_id == public_id)).one()
        assert access.access_cap(assembly) == access.DEMO_VOTERS
        assert access.attendees(db, assembly) == access.DEMO_VOTERS


def test_a_demo_session_takes_five_voters_with_codes(
    world: World, database: Database, keyring: Keyring
) -> None:
    user, public_id = opened_demo_session(database, keyring, access_mode=AccessMode.CODES)
    codes = world.codes(user, public_id, 7)
    for code in codes[: access.DEMO_VOTERS]:
        world.join(public_id, code)
    with pytest.raises(AccessDeniedError, match="demo_voters_full"):
        world.join(public_id, codes[access.DEMO_VOTERS])
    world.join(public_id, codes[0])  # a code in use moves to another phone, as anywhere


def test_a_revoked_batch_frees_no_place_in_a_demo_session(
    world: World, database: Database, keyring: Keyring
) -> None:
    """Counting only the usable codes, revoking a batch during a poll would let 5 more voters
    in, again and again (one batch of 5 after another)."""
    user, public_id = opened_demo_session(database, keyring, access_mode=AccessMode.CODES)
    first = world.codes(user, public_id, access.DEMO_VOTERS)
    second = world.codes(user, public_id, access.DEMO_VOTERS)
    for code in first:
        world.join(public_id, code)
    with database.write() as db:
        batch = db.scalars(
            select(CodeBatch)
            .join(Assembly, CodeBatch.assembly_id == Assembly.id)
            .where(Assembly.public_id == public_id)
            .order_by(CodeBatch.id)
        ).first()
        assert batch is not None
        access.revoke_code_batch(db, user, public_id, batch.id)
    with pytest.raises(AccessDeniedError, match="demo_voters_full"):
        world.join(public_id, second[0])


def test_real_sessions_keep_their_cap(world: World) -> None:
    owner = world.user()
    public_id = world.assembly(owner)
    assert len(world.voters(owner, public_id, access.DEMO_VOTERS + 1)) == access.DEMO_VOTERS + 1
