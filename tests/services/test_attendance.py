"""Attendance readings (FR-28): taken at chosen moments only, aggregated counts, never on a join
or a ballot, owner-scoped."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from agaura.models import AccessMode, Assembly, AuditEntry, User, VoterCredential
from agaura.services import access, assemblies, attendance, audit, polls
from agaura.services.attendance import Reading
from agaura.services.errors import NotFoundError
from tests.conftest import World

# Now, to the second: ballots cast during the test must find the poll open (its timer counts
# from its opening time).
T0 = dt.datetime.now(dt.UTC).replace(microsecond=0)


def at(minutes: int) -> dt.datetime:
    return T0 + dt.timedelta(minutes=minutes)


def timeline(world: World, owner: User, public_id: str) -> list[Reading]:
    with world.database.read() as db:
        return attendance.timeline(db, owner, public_id)


def audit_count(world: World) -> int:
    with world.database.read() as db:
        return db.scalar(select(func.count()).select_from(AuditEntry)) or 0


def test_readings_at_each_event(world: World, owner: User) -> None:
    public_id = world.assembly(owner, in_session=False)
    with world.database.write() as db:
        access.set_headcount(db, owner, public_id, 40, now=at(0))
        assemblies.open_assembly(db, owner, public_id, now=at(5))
    password = world.password(owner, public_id)
    tokens = [world.join(public_id, password) for _ in range(3)]
    with world.database.write() as db:
        access.set_headcount(db, owner, public_id, 42, now=at(10))
        poll = polls.create_poll(db, owner, public_id, polls.PollInput("Q", "approval", ["A", "B"]))
        polls.open_poll(db, owner, poll.id, minutes=5, now=at(15))
        first_id = poll.id
    world.vote(public_id, tokens[0], first_id, {"approved": [1]})
    world.join(public_id, password)  # a latecomer
    with world.database.write() as db:
        polls.close_expired_polls(db, now=at(21))  # by its timer
        second = polls.create_poll(
            db, owner, public_id, polls.PollInput("R", "approval", ["A", "B"])
        )
        polls.open_poll(db, owner, second.id, now=at(25))
        polls.close_poll(db, owner, second.id, now=at(30))
        assemblies.close_assembly(db, owner, public_id, now=at(40))
    assert timeline(world, owner, public_id) == [
        Reading(at(0), "headcount_set", 40, 0),
        Reading(at(5), "session_opened", 40, 0),
        Reading(at(10), "headcount_set", 42, 3),
        Reading(at(15), "poll_opened", 42, 3, first_id),
        Reading(at(21), "poll_closed", 42, 4, first_id),
        Reading(at(25), "poll_opened", 42, 4, second.id),
        Reading(at(30), "poll_closed", 42, 4, second.id),
        Reading(at(40), "session_closed", 42, 4),
    ]


def test_codes_mode_counts_activated_codes(world: World, owner: User) -> None:
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    codes = world.codes(owner, public_id, 5)
    for code in codes[:2]:
        world.join(public_id, code)
    with world.database.write() as db:
        access.set_headcount(db, owner, public_id, 2)
    assert timeline(world, owner, public_id)[-1].attendees == 2


def test_joins_and_ballots_leave_no_trace_in_the_log(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id)
    before = audit_count(world)
    tokens = world.voters(owner, public_id, 4)
    for token in tokens:
        world.vote(public_id, token, poll_id, {"choice": 1})
    assert audit_count(world) == before
    with world.database.write() as db:
        access.set_headcount(db, owner, public_id, 4)
        polls.close_poll(db, owner, poll_id)
    with world.database.read() as db:
        credential_ids = set(db.scalars(select(VoterCredential.id)))
        entries = db.scalars(select(AuditEntry)).all()
    allowed = {
        "public_id",
        "title",
        "method",
        "poll_id",
        "timer_minutes",
        "ballots",
        "ballots_sha256",
        "headcount",
        "attendees",
    }
    for entry in entries:
        assert set(entry.details) <= allowed, entry.details
        text = str(entry.details)
        assert "credential" not in text and "token" not in text
        assert not any(token in text for token in tokens)
    assert credential_ids  # the voters exist, the log never names them


def test_older_entries_read_as_blanks(world: World, owner: User) -> None:
    """Entries written before readings carried counts."""
    public_id = world.assembly(owner)
    with world.database.write() as db:
        assembly = db.scalar(select(Assembly).where(Assembly.public_id == public_id))
        assert assembly is not None
        audit.record(
            db, "poll.opened", user_id=owner.id, assembly_id=assembly.id, details={}, now=at(0)
        )
        audit.record(
            db, "assembly.closed", user_id=owner.id, assembly_id=assembly.id, details={"x": True}
        )
    readings = timeline(world, owner, public_id)
    assert readings[0] == Reading(at(0), "poll_opened", None, None, None)
    assert (readings[-1].event, readings[-1].headcount, readings[-1].attendees) == (
        "session_closed",
        None,
        None,
    )


def test_other_actions_take_no_reading(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    with world.database.write() as db:
        access.set_entries_locked(db, owner, public_id, True)
        access.set_access_mode(db, owner, public_id, AccessMode.CODES)
    assert [reading.event for reading in timeline(world, owner, public_id)] == ["session_opened"]


def test_another_account_gets_not_found(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    intruder = world.user("intruder@example.org")
    with pytest.raises(NotFoundError), world.database.read() as db:
        attendance.timeline(db, intruder, public_id)


@pytest.mark.parametrize(
    ("readings", "expected"),
    [
        ([], False),
        ([Reading(T0, "session_opened", None, 0)], False),
        ([Reading(T0, "session_opened", 30, 0)], True),
        ([Reading(T0, "headcount_set", None, 0)], True),
        ([Reading(T0, "session_opened", None, 0), Reading(T0, "poll_opened", None, 2)], True),
    ],
)
def test_worth_reporting(readings: list[Reading], expected: bool) -> None:
    assert attendance.worth_reporting(readings) is expected
