"""Application logs while voting through the web (SPEC §9.2): no ballot, no secret.

Every logger is captured down to DEBUG, the database engine's included, so that a log line
added on the vote path, or SQL parameters shown again (``hide_parameters``), fails here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agaura.models import User
from agaura.services.polls import PollInput
from agaura.web.live.routes import VOTER_COOKIE
from tests.conftest import RESOLUTION, World
from tests.web.live.conftest import APPROVAL, CONDORCET, MAJORITY_JUDGMENT, ballots_of, join
from tests.web.live.test_ballots import vote_through_summary

# The poll, the form a browser without JavaScript sends, and the ballot it stores.
FORMS: dict[str, tuple[PollInput, dict[str, Any], dict[str, Any]]] = {
    "single_choice": (RESOLUTION, {"choice": "2"}, {"choice": 2}),
    "approval": (APPROVAL, {"approved": ["1", "3"]}, {"approved": [1, 3]}),
    "majority_judgment": (
        MAJORITY_JUDGMENT,
        {"grade-01": "4", "grade-02": "0", "grade-03": "2"},
        {"grades": {"1": 4, "2": 0, "3": 2}},
    ),
    "condorcet": (
        CONDORCET,
        {"rank-01": "2", "rank-02": "1", "rank-03": "3", "rank-04": "4"},
        {"ranks": {"1": 2, "2": 1, "3": 3, "4": 4}},
    ),
}
FIELD_NAMES = ("choice", "approved", "grade-", "rank-", "grades", "ranks")


@pytest.mark.parametrize("method", FORMS)
def test_logs_never_hold_a_ballot_or_a_secret(
    app: FastAPI,
    client: TestClient,
    world: World,
    owner: User,
    session_id: str,
    method: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data, form, stored = FORMS[method]
    blank_voter = TestClient(app, follow_redirects=False)
    caplog.set_level(logging.DEBUG)
    # SQLAlchemy keeps its loggers at WARNING unless told otherwise: every statement, then.
    caplog.set_level(logging.INFO, logger="sqlalchemy.engine")
    join(client, world, owner, session_id)
    join(blank_voter, world, owner, session_id)
    poll_id = world.poll(owner, session_id, data)
    vote_through_summary(client, session_id, poll_id, form)
    vote_through_summary(blank_voter, session_id, poll_id, {"blank": "1"})
    assert sorted(ballots_of(world, poll_id), key=lambda row: row[0]) == [
        (False, stored),
        (True, None),
    ]

    logged = "\n".join(
        f"{record.name} {record.getMessage()} {record.args!r}" for record in caplog.records
    )
    # The capture works: the statement that stores each ballot is there, its values hidden.
    assert "INSERT INTO ballot" in logged and "parameters hidden" in logged
    for name in FIELD_NAMES:
        assert name not in logged, name
    assert json.dumps(stored) not in logged and json.dumps(stored, sort_keys=True) not in logged
    assert world.password(owner, session_id) not in logged
    for voter in (client, blank_voter):
        token = voter.cookies.get(VOTER_COOKIE)
        assert token and token not in logged
