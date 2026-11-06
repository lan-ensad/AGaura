"""Crash test (PLAN P2): a hard kill during a voting burst loses no confirmed ballot."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from sqlalchemy import func, select, text

from agaura.models import Ballot, Participation, User
from agaura.services import access
from tests.conftest import World

VOTERS = 300
KILL_AFTER = 60

CHILD = """
import json, sys
from agaura.db import Database
from agaura.services import ballots

database = Database(sys.argv[1])
public_id, poll_id, tokens = json.load(open(sys.argv[2]))
for index, token in enumerate(tokens):
    with database.write() as db:
        ballots.cast_vote(db, public_id, token, poll_id, {"choice": 1 + index % 2})
    print(index, flush=True)  # the voter would now see the confirmation
"""


def test_hard_kill_during_a_voting_burst(world: World, owner: User, tmp_path: Path) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id)
    tokens = world.voters(owner, public_id, VOTERS)
    work = tmp_path / "work.json"
    work.write_text(json.dumps([public_id, poll_id, tokens]))

    child = subprocess.Popen(  # noqa: S603 (fixed command, test only)
        [sys.executable, "-c", CHILD, str(world.database.url), str(work)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    confirmed = []
    for line in child.stdout:
        confirmed.append(int(line))
        if len(confirmed) == KILL_AFTER:
            os.kill(child.pid, signal.SIGKILL)
            break
    confirmed.extend(int(line) for line in child.stdout)  # printed before the kill
    assert child.wait() == -signal.SIGKILL
    assert KILL_AFTER <= len(confirmed) < VOTERS  # killed mid-burst

    with world.database.read() as db:
        assert db.scalar(text("PRAGMA integrity_check")) == "ok"
        for index in confirmed:
            credential = access.credential_for(db, public_id, tokens[index])
            assert credential is not None
            voted = db.scalar(
                select(Participation.poll_id).where(
                    Participation.poll_id == poll_id, Participation.credential_id == credential.id
                )
            )
            assert voted == poll_id, f"confirmed vote {index} lost"
        participations = db.scalar(select(func.count()).select_from(Participation))
        ballots_count = db.scalar(select(func.count()).select_from(Ballot))
    # Atomicity: every participation has its ballot, and nothing beyond the last commit.
    assert participations == ballots_count
    assert len(confirmed) <= (participations or 0) <= len(confirmed) + 1
