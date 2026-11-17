"""An aura battle in the record (SPEC §6.8, FR-61, FR-62): the minutes mention it in the course
of the session, one line with its title, and nothing else carries it: no number, no result,
no attendance reading, no line of the session export."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from agaura.exports import json_export
from agaura.models import User
from agaura.services import exports, meeting, polls
from agaura.services.polls import PollInput
from agaura.voting.aura import Taps
from tests.conftest import RESOLUTION, World
from tests.exports.test_minutes import minutes_html, text_of

BATTLE = PollInput("Thème de la fête", "aura", ["Pirates", "Disco"], {"duration": 5})
START = dt.datetime(2026, 6, 20, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def session(world: World, owner: User) -> tuple[str, int, int]:
    """A resolution, then a battle while the second agenda item is under discussion."""
    public_id = world.assembly(owner, title="AG 2026")
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, ["Rapport moral", "Fête de l'été"])
        meeting.set_current_item(db, owner, public_id, 1)
    ids = []
    for minute, data in enumerate((RESOLUTION, BATTLE)):
        with world.database.write() as db:
            if minute:
                meeting.set_current_item(db, owner, public_id, 2)
            poll = polls.create_poll(db, owner, public_id, data)
            polls.open_poll(db, owner, poll.id, now=START + dt.timedelta(minutes=minute))
            polls.close_poll(db, owner, poll.id, taps_of=lambda _id: Taps({1: 9, 2: 40}, phones=4))
            ids.append(poll.id)
    return public_id, ids[0], ids[1]


def test_minutes_mention_it_only(world: World, owner: User, session: tuple[str, int, int]) -> None:
    public_id, _real, _battle = session
    text = text_of(minutes_html(world, owner, public_id))
    assert "Battle d'aura\u00a0: «\u00a0Thème de la fête\u00a0»" in text
    assert text.count("Thème de la fête") == 1  # not in the list of polls
    assert "Disco" not in text and "Pirates" not in text  # no result
    assert "Vote n° 1" in text and "Vote n° 2" not in text
    # Placed under the item under discussion when it was pushed.
    assert text.index("Fête de l'été", text.index("Déroulé")) < text.index("Battle d'aura")


def test_session_export_leaves_it_out(
    world: World, owner: User, session: tuple[str, int, int]
) -> None:
    public_id, real, battle = session
    with world.database.read() as db:
        record = exports.session_record(db, owner, public_id, with_ballots=True)
        assert {reading.poll_id for reading in record.attendance} <= {None, real}
        document = json.loads(
            json_export.dumps(json_export.document(record, record.finished, now=START))
        )
    assert [poll["definition"]["title"] for poll in document["polls"]] == [RESOLUTION.title]
    assert battle not in [poll["id"] for poll in document["polls"]]
