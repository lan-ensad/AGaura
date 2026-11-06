import datetime as dt
import threading
import uuid
from collections import Counter
from typing import Any

import pytest
from sqlalchemy import func, select

from agaura.models import Ballot, Participation, Poll, User, utcnow
from agaura.services import ballots, polls, results
from agaura.services.errors import (
    AccessDeniedError,
    AlreadyVotedError,
    InvalidInputError,
    InvalidStateError,
    NotFoundError,
)
from agaura.services.polls import PollInput
from tests.conftest import World

GRADING = PollInput("Projets", "majority_judgment", ["Jardin", "Fresque", "Navette"])


def stored(world: World, poll_id: int) -> tuple[int, list[Ballot]]:
    with world.database.read() as db:
        participations = db.scalar(
            select(func.count()).select_from(Participation).where(Participation.poll_id == poll_id)
        )
        rows = list(db.scalars(select(Ballot).where(Ballot.poll_id == poll_id)))
    return participations or 0, rows


class TestCastVote:
    def test_valid_and_blank_ballots(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, GRADING)
        first, second = world.voters(owner, public_id, 2)
        world.vote(public_id, first, poll_id, {"grades": {"1": 0, "3": 2}})
        world.vote(public_id, second, poll_id, None)
        participations, rows = stored(world, poll_id)
        assert participations == 2
        contents = sorted((row.is_blank, str(row.content)) for row in rows)
        assert contents == [
            (False, str({"grades": {"1": 0, "2": 5, "3": 2}})),  # normalized
            (True, "None"),
        ]
        with world.database.read() as db:
            credential = db.scalar(select(Participation.credential_id).limit(1))
            assert credential is not None

    def test_invalid_ballot_is_rejected_with_its_reason(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id)
        token = world.voters(owner, public_id, 1)[0]
        with pytest.raises(InvalidInputError, match="unknown_option"):
            world.vote(public_id, token, poll_id, {"choice": 3})
        assert stored(world, poll_id) == (0, [])
        world.vote(public_id, token, poll_id, {"choice": 2})  # can still vote

    def test_second_ballot_is_refused(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id)
        token = world.voters(owner, public_id, 1)[0]
        world.vote(public_id, token, poll_id, {"choice": 1})
        with pytest.raises(AlreadyVotedError):
            world.vote(public_id, token, poll_id, {"choice": 2})
        participations, rows = stored(world, poll_id)
        assert participations == 1 and [row.content for row in rows] == [{"choice": 1}]

    def test_poll_must_be_open(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        token = world.voters(owner, public_id, 1)[0]
        draft = world.poll(owner, public_id, open_it=False)
        closed = world.poll(owner, public_id)
        with world.database.write() as db:
            polls.close_poll(db, owner, closed)
        for poll_id in (draft, closed):
            with pytest.raises(InvalidStateError, match="poll_not_open"):
                world.vote(public_id, token, poll_id, {"choice": 1})

    def test_timer_is_enforced_before_the_background_job_runs(
        self, world: World, owner: User
    ) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, open_it=False)
        token = world.voters(owner, public_id, 1)[0]
        start = utcnow()
        with world.database.write() as db:
            polls.open_poll(db, owner, poll_id, minutes=1, now=start)
        with pytest.raises(InvalidStateError), world.database.write() as db:
            later = start + dt.timedelta(minutes=1)
            ballots.cast_vote(db, public_id, token, poll_id, {"choice": 1}, now=later)

    def test_credential_and_poll_must_match(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        other_id = world.assembly(owner)
        other_poll = world.poll(owner, other_id)
        token = world.voters(owner, public_id, 1)[0]
        with pytest.raises(NotFoundError):
            world.vote(public_id, token, other_poll, {"choice": 1})
        with pytest.raises(NotFoundError):
            world.vote(public_id, token, 999, {"choice": 1})
        with pytest.raises(AccessDeniedError, match="invalid_credential"):
            world.vote(other_id, token, other_poll, {"choice": 1})
        with pytest.raises(AccessDeniedError):
            world.vote(public_id, None, other_poll, {"choice": 1})  # type: ignore[arg-type]

    def test_participation_and_ballot_are_atomic(
        self, world: World, owner: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id)
        first, second = world.voters(owner, public_id, 2)
        world.vote(public_id, first, poll_id, {"choice": 1})
        existing_id = stored(world, poll_id)[1][0].id
        # Force the ballot insert to fail after the participation was added.
        monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(existing_id))
        with pytest.raises(AlreadyVotedError):
            world.vote(public_id, second, poll_id, {"choice": 2})
        assert stored(world, poll_id)[0] == 1  # the participation was rolled back too
        monkeypatch.undo()
        world.vote(public_id, second, poll_id, {"choice": 2})
        assert stored(world, poll_id)[0] == 2


def test_fifty_concurrent_submissions_with_one_credential(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id)
    token = world.voters(owner, public_id, 1)[0]
    barrier = threading.Barrier(50)
    outcomes: Counter[str] = Counter()
    lock = threading.Lock()

    def submit(choice: int) -> None:
        barrier.wait()
        try:
            world.vote(public_id, token, poll_id, {"choice": choice})
            outcome = "recorded"
        except AlreadyVotedError:
            outcome = "already_voted"
        except Exception as error:  # reported by the assertion below
            outcome = repr(error)
        with lock:
            outcomes[outcome] += 1

    threads = [threading.Thread(target=submit, args=(1 + i % 2,)) for i in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes == {"recorded": 1, "already_voted": 49}
    participations, rows = stored(world, poll_id)
    assert participations == 1 and len(rows) == 1


def test_digest_matches_the_canonical_form(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, GRADING)
    for token in world.voters(owner, public_id, 5):
        world.vote(public_id, token, poll_id, {"grades": {"1": 1, "2": 2, "3": 0}})
    with world.database.write() as db:
        result = polls.close_poll(db, owner, poll_id)
    with world.database.read() as db:
        poll = db.get(Poll, poll_id)
        assert poll is not None
        rows = list(db.scalars(select(Ballot).where(Ballot.poll_id == poll_id)))
        document: dict[str, Any] = {
            "poll": results.canonical_poll(poll),
            "ballots": results.canonical_ballots(reversed(rows)),
        }
        assert results.ballots_sha256(poll, rows) == result.ballots_sha256
    assert [ballot["id"] for ballot in document["ballots"]] == sorted(row.id for row in rows)
    assert document["poll"]["options"][0] == {"id": 1, "label": "Jardin", "description": None}
    assert result.data["outcome"]["winner"] == 3
    assert result.data["attendees"] == 5


def test_turnout(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id)
    for token in world.voters(owner, public_id, 3):
        world.vote(public_id, token, poll_id, None)
    with world.database.read() as db:
        assert results.turnout(db, poll_id) == 3
