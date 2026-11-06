"""Helpers for the projection screen: a session in session and its secret screen link."""

from __future__ import annotations

from typing import Any

import pytest

from agaura.models import User
from agaura.services import access, polls
from agaura.services.polls import PollInput
from tests.conftest import World

# The icon that may lead a word of the screen (macros/icons.html: the state of entries, of a
# question put to the vote, the chronometer's label), for the patterns of the tests.
ICON = r"(?:<svg[^>]*>\s*<use[^>]*></use></svg>)?"


def titled(title: str, *options: str) -> PollInput:
    return PollInput(title=title, method="single_choice", options=list(options or ("Oui", "Non")))


def screen_url(world: World, owner: User, public_id: str) -> str:
    with world.database.read() as db:
        token = access.screen_token(db, world.keyring, owner, public_id)
    return f"/{public_id}/screen/{token}"


def poll_action(world: World, owner: User, action: str, poll_id: int, **kwargs: Any) -> None:
    with world.database.write() as db:
        getattr(polls, action)(db, owner, poll_id, **kwargs)


def access_action(world: World, owner: User, action: str, *args: Any) -> None:
    with world.database.write() as db:
        getattr(access, action)(db, *args)


@pytest.fixture
def session_id(world: World, owner: User) -> str:
    return world.assembly(
        owner, title="AG de l'association", registered_count=100, quorum_rule="1/4"
    )


@pytest.fixture
def url(world: World, owner: User, session_id: str) -> str:
    return screen_url(world, owner, session_id)
