"""Aura battles in the services (SPEC §6.8): pushing, the stage to itself, closing with the
taps, no ballot and nothing per phone, out of the record."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from agaura.models import Ballot, Participation, Poll, PollStatus, User, utcnow
from agaura.services import exports, polls
from agaura.services.errors import InvalidInputError, InvalidStateError
from agaura.services.polls import PollInput
from agaura.voting.aura import CHOOSE_SECONDS, GRACE_SECONDS, Taps
from tests.conftest import RESOLUTION, World

BATTLE = PollInput("Thème de la fête", "aura", ["Pirates", "Disco", "Western"], {"duration": 10})


def get(world: World, owner: User, poll_id: int) -> Poll:
    with world.database.read() as db:
        poll = polls.get_poll(db, owner, poll_id)
        _ = poll.options, poll.result
        return poll


def test_push_sets_the_end(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, BATTLE, open_it=False)
    now = utcnow()
    with world.database.write() as db:
        polls.open_poll(db, owner, poll_id, now=now)
    poll = get(world, owner, poll_id)
    assert poll.status is PollStatus.OPEN
    assert poll.closes_at == now + dt.timedelta(seconds=CHOOSE_SECONDS + 10)
    assert not poll.on_record and poll.is_aura


def test_no_timer(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, BATTLE, open_it=False)
    with world.database.write() as db, pytest.raises(InvalidInputError, match="invalid_timer"):
        polls.open_poll(db, owner, poll_id, minutes=2)


def test_the_stage_to_itself(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    resolution = world.poll(owner, public_id)
    battle = world.poll(owner, public_id, BATTLE, open_it=False)
    with world.database.write() as db, pytest.raises(InvalidStateError, match="aura_needs_stage"):
        polls.open_poll(db, owner, battle)
    with world.database.write() as db:
        polls.close_poll(db, owner, resolution)
        polls.open_poll(db, owner, battle)
    other = world.poll(owner, public_id, RESOLUTION, open_it=False)
    with world.database.write() as db, pytest.raises(InvalidStateError, match="aura_running"):
        polls.open_poll(db, owner, other)


def test_a_timer_run_out_leaves_the_stage(world: World, owner: User) -> None:
    """A poll past its timer, not yet closed by the periodic job, accepts no vote any more."""
    public_id = world.assembly(owner)
    resolution = world.poll(owner, public_id, open_it=False)
    battle = world.poll(owner, public_id, BATTLE, open_it=False)
    now = utcnow()
    with world.database.write() as db:
        polls.open_poll(db, owner, resolution, minutes=1, now=now - dt.timedelta(minutes=2))
        polls.open_poll(db, owner, battle, now=now)


def test_closing_ranks_the_taps_and_shows_them(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    world.voters(owner, public_id, 3)
    poll_id = world.poll(owner, public_id, BATTLE)
    taps = Taps({1: 30, 2: 75}, phones=3)
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id, taps_of=lambda _poll_id: taps)
    poll = get(world, owner, poll_id)
    assert poll.status is PollStatus.CLOSED
    assert poll.shown_on_screen and poll.screen_shown_at is not None  # by itself (FR-75)
    assert poll.result is not None
    data = poll.result.data
    assert data["method"] == "aura"
    assert data["counts"] == {"cast": 0, "blank": 0, "valid": 0}
    assert data["attendees"] == 3
    outcome = data["outcome"]
    assert (outcome["status"], outcome["winners"], outcome["taps"]) == ("decided", [2], 105)
    assert (outcome["phones"], outcome["interrupted"]) == (3, False)
    with world.database.read() as db:
        assert db.scalar(select(func.count()).select_from(Ballot)) == 0
        assert db.scalar(select(func.count()).select_from(Participation)) == 0


def test_taps_lost_say_interrupted(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, BATTLE)
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)  # the arena never saw it
    result = get(world, owner, poll_id).result
    assert result is not None
    assert result.data["outcome"]["interrupted"] is True
    assert result.data["outcome"]["status"] == "no_valid_votes"


def test_closed_by_itself_after_the_grace(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, BATTLE)
    end = get(world, owner, poll_id).closes_at
    assert end is not None
    seen: list[int] = []

    def taps_of(found: int) -> Taps:
        seen.append(found)
        return Taps({3: 4}, phones=1)

    with world.database.write() as db:
        assert polls.close_expired_polls(db, taps_of=taps_of, now=end) == []
        later = end + dt.timedelta(seconds=GRACE_SECONDS)
        assert [poll.id for poll in polls.close_expired_polls(db, taps_of=taps_of, now=later)] == [
            poll_id
        ]
    assert seen == [poll_id]
    result = get(world, owner, poll_id).result
    assert result is not None
    assert result.data["outcome"]["winners"] == [3]


def test_out_of_the_record(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    first = world.poll(owner, public_id)
    with world.database.write() as db:
        polls.close_poll(db, owner, first)
    battle = world.poll(owner, public_id, BATTLE)
    with world.database.write() as db:
        polls.close_poll(db, owner, battle)
        polls.publish_result(db, owner, battle, voters=True)
    last = world.poll(owner, public_id)
    with world.database.read() as db:
        record = exports.session_record(db, owner, public_id)
        assert [item.poll.id for item in record.polls] == [first, last]
        assert [item.number for item in record.polls] == [1, 2]
        assert [poll.id for poll in record.battles] == [battle]
        assert exports.poll_numbers(db, record.assembly) == {first: 1, last: 2}
        assert battle not in [poll.id for poll in exports.public_results(db, record.assembly)]
        with pytest.raises(InvalidStateError, match="export_aura_poll"):
            exports.poll_record(db, owner, public_id, battle)
