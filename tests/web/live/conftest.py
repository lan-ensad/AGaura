"""Helpers for the voter pages: a session in session, voters joining through the web."""

from __future__ import annotations

import re
from collections.abc import Iterator
from itertools import pairwise
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agaura.models import Ballot, User
from agaura.services import polls
from agaura.services.polls import PollInput
from agaura.web.live.routes import VOTER_COOKIE
from tests.conftest import World
from tests.web.conftest import post

APPROVAL = PollInput(
    title="Élection du conseil",
    method="approval",
    options=["Alice", "Bruno", "Chloé"],
    params={"seats": 2, "max_choices": 2},
)
MAJORITY_JUDGMENT = PollInput(
    title="Projets de quartier",
    method="majority_judgment",
    options=["Jardin", "Fresque", "Banc"],
)
CONDORCET = PollInput(
    title="Nom de la lettre",
    method="condorcet",
    options=["Gazette", "Écho", "Lien", "Fil"],
)


@pytest.fixture
def session_id(world: World, owner: User) -> str:
    return world.assembly(owner, title="AG de l'association")


@pytest.fixture
def other_client(app: FastAPI) -> Iterator[TestClient]:
    """A second phone. No ``with``: the application is already started by ``client``."""
    yield TestClient(app, follow_redirects=False)


def join(client: TestClient, world: World, owner: User, public_id: str) -> None:
    response = post(client, f"/{public_id}/join", {"secret": world.password(owner, public_id)})
    assert response.status_code == 303, response.text
    assert client.cookies.get(VOTER_COOKIE)


def poll_action(world: World, owner: User, action: str, poll_id: int, **kwargs: Any) -> None:
    with world.database.write() as db:
        getattr(polls, action)(db, owner, poll_id, **kwargs)


def ballots_of(world: World, poll_id: int) -> list[tuple[bool, Any]]:
    with world.database.read() as db:
        rows = db.query(Ballot).filter(Ballot.poll_id == poll_id).all()
        return [(ballot.is_blank, ballot.content) for ballot in rows]


def headings(html: str) -> list[tuple[int, str]]:
    """Level and text of every heading of a page, in order."""
    return [
        (int(level), re.sub(r"<[^>]+>", "", text).strip())
        for level, text in re.findall(r"<h([1-6])\b[^>]*>(.*?)</h\1>", html, re.S)
    ]


def assert_outline(html: str, title: str) -> list[tuple[int, str]]:
    """One level-one heading, the session title of the top bar, first; no level skipped."""
    found = headings(html)
    assert found[0] == (1, title), found
    assert [level for level, _ in found].count(1) == 1, found
    for (before, _), (after, text) in pairwise(found):
        assert after <= before + 1, (text, found)
    return found
