"""Aura battles on the voter side (SPEC §6.8): the section of the live page, the taps route,
and nothing stored, logged or announced per phone."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agaura import tasks
from agaura.aura import Arena
from agaura.models import AuditEntry, Ballot, Participation, Poll, User
from agaura.services import polls
from agaura.services.polls import PollInput
from agaura.voting.aura import CHOOSE_SECONDS
from agaura.web import aura
from agaura.web.security import VOTER_COOKIE
from tests.conftest import RESOLUTION, World
from tests.web.conftest import csrf_token, post
from tests.web.live.conftest import join

BATTLE = PollInput("Thème de la fête", "aura", ["Pirates", "Disco", "Western"], {"duration": 5})


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(app: FastAPI) -> Clock:
    clock = Clock()
    app.state.arena.clock = clock
    return clock


@pytest.fixture
def arena(app: FastAPI) -> Arena:
    return app.state.arena


@pytest.fixture
def voter(client: TestClient, world: World, owner: User, session_id: str) -> TestClient:
    join(client, world, owner, session_id)
    return client


def push(
    world: World, owner: User, public_id: str, arena: Arena, clock: Clock
) -> tuple[int, float]:
    """A battle pushed and followed by the arena, as the console does; (poll id, opened at)."""
    poll_id = world.poll(owner, public_id, BATTLE)
    with world.database.read() as db:
        opened = db.get_one(Poll, poll_id).opened_at
    assert opened is not None
    clock.now = opened.timestamp()
    aura.sync_battles(world.database, arena, public_id)
    return poll_id, opened.timestamp()


def taps(client: TestClient, public_id: str, poll_id: int, body: bytes, **headers: str):
    return client.post(
        f"/{public_id}/aura/{poll_id}",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRF-Token": csrf_token(client),
            **headers,
        },
    )


def counts(world: World) -> tuple[int, int, int]:
    with world.database.read() as db:
        return tuple(  # type: ignore[return-value]
            db.scalar(select(func.count()).select_from(table)) or 0
            for table in (Participation, Ballot, AuditEntry)
        )


def test_the_live_page_draws_the_battle(
    voter: TestClient, world: World, owner: User, session_id: str, arena: Arena, clock: Clock
) -> None:
    poll_id, _opened = push(world, owner, session_id, arena, clock)
    html = voter.get(f"/{session_id}").text
    section = re.search(r'<section class="card poll poll-aura".*?</section>', html, re.S)
    assert section is not None
    text = section.group(0)
    assert f'data-tap-url="/{session_id}/aura/{poll_id}"' in text
    assert f'data-key="poll-{poll_id}-aura"' in text
    choose = int(re.search(r'data-choose-ms="(\d+)"', text).group(1))  # type: ignore[union-attr]
    end = int(re.search(r'data-end-ms="(\d+)"', text).group(1))  # type: ignore[union-attr]
    assert 0 < choose <= CHOOSE_SECONDS * 1000
    assert end - choose == pytest.approx(5000, abs=50)
    assert text.count('type="radio"') == 3
    assert "<form" not in text  # nothing is ever submitted
    assert "besoin de JavaScript" in text
    assert "js/aura.js" in html


def test_no_ballot_step(
    voter: TestClient, world: World, owner: User, session_id: str, arena: Arena, clock: Clock
) -> None:
    poll_id, _opened = push(world, owner, session_id, arena, clock)
    assert post(voter, f"/{session_id}/polls/{poll_id}/review", {"choice": "01"}).status_code == 404
    assert post(voter, f"/{session_id}/polls/{poll_id}", {"choice": "01"}).status_code == 404


def test_taps_counted_during_the_tap_phase_only(
    voter: TestClient, world: World, owner: User, session_id: str, arena: Arena, clock: Clock
) -> None:
    poll_id, opened = push(world, owner, session_id, arena, clock)
    before = counts(world)
    clock.now = opened + 1  # still choosing
    assert taps(voter, session_id, poll_id, b"o=02&n=05").status_code == 409
    clock.now = opened + CHOOSE_SECONDS + 1
    response = taps(voter, session_id, poll_id, b"o=02&n=05")
    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert taps(voter, session_id, poll_id, b"o=09&n=05").status_code == 204  # counts nothing
    view = arena.view(poll_id)
    assert view == {"levels": [0, 5, 0], "totals": [0, 5, 0], "phones": 1}
    assert counts(world) == before  # no participation, no ballot, no log line

    clock.now = opened + CHOOSE_SECONDS + 5 + 1  # past the end and the grace
    assert taps(voter, session_id, poll_id, b"o=02&n=05").status_code == 409
    later = dt.datetime.fromtimestamp(clock.now, dt.UTC)
    assert tasks.close_expired_polls(world.database, None, arena, now=later) == 1
    assert arena.battle(poll_id) is None
    with world.database.read() as db:
        poll = polls.get_poll(db, owner, poll_id)
        assert poll.result is not None
        outcome = poll.result.data["outcome"]
        assert (outcome["winners"], outcome["taps"], outcome["phones"]) == ([2], 5, 1)
        assert poll.shown_on_screen


@pytest.mark.parametrize(
    "body", [b"", b"o=2&n=5", b"o=02&n=5", b"o=02&n=05&x=1", b"n=05&o=02", b"o=ab&n=05"]
)
def test_one_body_shape(
    voter: TestClient,
    world: World,
    owner: User,
    session_id: str,
    arena: Arena,
    clock: Clock,
    body: bytes,
) -> None:
    poll_id, opened = push(world, owner, session_id, arena, clock)
    clock.now = opened + CHOOSE_SECONDS + 1
    assert taps(voter, session_id, poll_id, body).status_code == 422


def test_refused_without_csrf_or_credential(
    client: TestClient,
    voter: TestClient,
    other_client: TestClient,
    world: World,
    owner: User,
    session_id: str,
    arena: Arena,
    clock: Clock,
) -> None:
    poll_id, opened = push(world, owner, session_id, arena, clock)
    clock.now = opened + CHOOSE_SECONDS + 1
    forged = voter.post(f"/{session_id}/aura/{poll_id}", content=b"o=01&n=05")
    assert forged.status_code == 403
    assert taps(other_client, session_id, poll_id, b"o=01&n=05").status_code == 401
    view = arena.view(poll_id)
    assert view is not None and view["totals"] == [0, 0, 0]


def test_a_phone_of_another_session(
    voter: TestClient,
    world: World,
    owner: User,
    session_id: str,
    arena: Arena,
    clock: Clock,
) -> None:
    other = world.assembly(owner, title="Autre")
    poll_id, opened = push(world, owner, other, arena, clock)
    clock.now = opened + CHOOSE_SECONDS + 1
    # The cookie of ``session_id`` is scoped to its path: under the other session's path, none.
    assert taps(voter, other, poll_id, b"o=01&n=05").status_code == 401
    # Under its own session's path, the other session's battle is not found there.
    assert taps(voter, session_id, poll_id, b"o=01&n=05").status_code == 409


def test_taken_up_after_a_restart(
    app: FastAPI,
    voter: TestClient,
    world: World,
    owner: User,
    session_id: str,
    arena: Arena,
    clock: Clock,
) -> None:
    """The arena lost the battle: the first batch takes it up, and says taps may be lost."""
    poll_id, opened = push(world, owner, session_id, arena, clock)
    arena.end(poll_id)
    clock.now = opened + CHOOSE_SECONDS + 1
    assert taps(voter, session_id, poll_id, b"o=03&n=04").status_code == 204
    battle = arena.battle(poll_id)
    assert battle is not None and battle.interrupted
    assert battle.totals == {3: 4}


def test_the_console_follows_battles(
    admin: TestClient, world: World, owner: User, session_id: str, arena: Arena, clock: Clock
) -> None:
    """Pushed from the console, a battle enters the arena; cancelled, it leaves it."""
    poll_id = world.poll(owner, session_id, BATTLE, open_it=False)
    response = post(admin, f"/manage/{session_id}/polls/{poll_id}/open")
    assert response.status_code == 303
    assert arena.polls_of(session_id) == [poll_id]
    response = post(admin, f"/manage/{session_id}/polls/{poll_id}/cancel", {"reason": "Essai"})
    assert response.status_code == 303
    assert arena.polls_of(session_id) == []


def test_stopped_early_keeps_the_taps(
    admin: TestClient,
    other_client: TestClient,
    world: World,
    owner: User,
    session_id: str,
    arena: Arena,
    clock: Clock,
) -> None:
    join(other_client, world, owner, session_id)
    poll_id, opened = push(world, owner, session_id, arena, clock)
    clock.now = opened + CHOOSE_SECONDS + 1
    assert taps(other_client, session_id, poll_id, b"o=01&n=07").status_code == 204
    assert post(admin, f"/manage/{session_id}/polls/{poll_id}/close").status_code == 303
    assert arena.battle(poll_id) is None
    with world.database.read() as db:
        result = polls.get_poll(db, owner, poll_id).result
        assert result is not None
        assert result.data["outcome"]["taps"] == 7


def test_every_request_has_one_size() -> None:
    """Two digits each: the size of a batch never tells the option (SPEC §9.2)."""
    sizes: set[int] = set()
    make: Callable[[int, int], str] = lambda option, n: f"o={option:02d}&n={n:02d}"  # noqa: E731
    for option in range(1, 7):
        for n in range(1, 100):
            sizes.add(len(make(option, n)))
    assert sizes == {9}
    assert aura.TAPS.fullmatch(make(6, 99).encode())


def test_a_cookie_no_longer_valid(
    voter: TestClient, world: World, owner: User, session_id: str, arena: Arena, clock: Clock
) -> None:
    """A device that left the session (or a forged cookie) taps for nothing."""
    poll_id, opened = push(world, owner, session_id, arena, clock)
    clock.now = opened + CHOOSE_SECONDS + 1
    voter.cookies.set(VOTER_COOKIE, "x" * 43, path=f"/{session_id}")
    assert taps(voter, session_id, poll_id, b"o=01&n=05").status_code == 401
    view = arena.view(poll_id)
    assert view is not None and view["totals"] == [0, 0, 0]


def test_not_a_battle(voter: TestClient, world: World, owner: User, session_id: str) -> None:
    """Taps for an ordinary poll open are refused: it has no battle to count them."""
    poll_id = world.poll(owner, session_id, RESOLUTION)
    assert taps(voter, session_id, poll_id, b"o=01&n=05").status_code == 409
