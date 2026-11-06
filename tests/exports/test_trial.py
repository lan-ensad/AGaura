"""The trial vote of the checklist (D-I, FR-61, FR-62): run like any poll, left out of the record.

It gets no number, and the minutes, the session export, the public results and the attendance
readings they carry leave it out; the recount of an export still checks every poll the file
holds. Its own exports answer 409: tests/web/manage/test_exports.py."""

from __future__ import annotations

import datetime as dt
import io
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agaura.exports import json_export, recount
from agaura.models import User
from agaura.services import access, exports, polls
from agaura.services.polls import PollInput
from tests.conftest import RESOLUTION, World
from tests.exports.conftest import vote_all
from tests.exports.test_minutes import minutes_html, text_of

TRIAL = PollInput("Tout le monde est-il connecté\u00a0?", "single_choice", ["Oui", "Non"])
START = dt.datetime(2026, 6, 20, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def session(world: World, owner: User) -> tuple[str, int, int]:
    return build_trial_session(world, owner)


def build_trial_session(world: World, owner: User) -> tuple[str, int, int]:
    """A trial vote pushed first, voted, closed and published, then a real resolution; the
    headcount saved between them, so the attendance record says more than the opening.
    Returns the session ID, the trial's id and the resolution's id."""
    public_id = world.assembly(owner, title="AG 2026")
    tokens = world.voters(owner, public_id, 3)
    ids = []
    for minute, (data, trial) in enumerate(((TRIAL, True), (RESOLUTION, False))):
        with world.database.write() as db:
            poll = polls.create_poll(db, owner, public_id, data, trial=trial)
            polls.open_poll(db, owner, poll.id, now=START + dt.timedelta(minutes=minute))
            ids.append(poll.id)
        vote_all(world, public_id, tokens, poll.id, [{"choice": 1}, {"choice": 1}, {"choice": 2}])
        with world.database.write() as db:
            polls.close_poll(db, owner, poll.id)
            polls.publish_result(db, owner, poll.id, voters=True)
            if trial:
                access.set_headcount(db, owner, public_id, 3)
    return public_id, ids[0], ids[1]


def test_no_number_and_no_reading(world: World, owner: User, session: tuple[str, int, int]) -> None:
    public_id, _trial, real = session
    with world.database.read() as db:
        assembly = exports.session_record(db, owner, public_id).assembly
        assert exports.poll_numbers(db, assembly) == {real: 1}
        record = exports.session_record(db, owner, public_id, with_ballots=True)
        assert [item.poll.id for item in record.polls] == [real]
        assert {reading.poll_id for reading in record.attendance} == {None, real}
        assert [poll.id for poll in exports.public_results(db, assembly)] == [real]


def test_minutes_leave_it_out(world: World, owner: User, session: tuple[str, int, int]) -> None:
    public_id, _, _ = session
    text = text_of(minutes_html(world, owner, public_id))
    assert "Tout le monde est-il connecté\u00a0?" not in text
    assert "Approbation des comptes" in text and "Vote n° 1" in text
    assert "Vote n° 2" not in text and "Vote n° ?" not in text
    assert "Effectif saisi" in text  # the attendance record stays, without the trial's lines


def test_session_export_leaves_it_out_and_recounts(
    tmp_path: Path, world: World, owner: User, session: tuple[str, int, int]
) -> None:
    public_id, _trial, real = session
    with world.database.read() as db:
        record = exports.session_record(db, owner, public_id, with_ballots=True)
        document = json_export.document(record, record.finished, now=START)
    data = json.loads(json_export.dumps(document))
    assert [(poll["id"], poll["number"]) for poll in data["polls"]] == [(real, 1)]
    assert "Tout le monde est-il connecté\u00a0?" not in json.dumps(data, ensure_ascii=False)
    readings = data["session"]["attendance"]
    assert [reading["poll_number"] for reading in readings if reading["poll_number"]] == [1, 1]
    path = tmp_path / "export.json"
    path.write_bytes(json_export.dumps(document))
    out = io.StringIO()
    assert recount.main([str(path)], out=out) == 0, out.getvalue()
    assert out.getvalue().count(": OK") == 1


def test_a_copy_stays_a_trial(world: World, owner: User, session: tuple[str, int, int]) -> None:
    _public_id, trial, real = session
    with world.database.write() as db:
        copy = polls.duplicate_poll(db, owner, trial)
        other = polls.duplicate_poll(db, owner, real)
        assert copy.trial and not other.trial
        renamed = replace(TRIAL, title="Encore un essai")
        assert polls.update_poll(db, owner, copy.id, renamed).trial  # editing keeps the flag
