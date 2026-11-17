"""An aura battle on the projection screen (SPEC §6.8): the stage to itself while it runs, its
phases on the server's clock, the gauges and totals as the arena holds them, then its ranking
shown by itself once closed, with the taps of every option whatever their number."""

from __future__ import annotations

import datetime as dt
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agaura.models import Poll, User, utcnow
from agaura.services import assemblies, polls
from agaura.services import screen as screen_service
from agaura.services.polls import PollInput
from agaura.voting.aura import CHOOSE_SECONDS, Taps
from tests.conftest import RESOLUTION, World
from tests.web.screen.conftest import poll_action
from tests.web.screen.test_screen import RESULT_TEXTS, strict_page

BATTLE = PollInput("Thème de la fête", "aura", ["Pirates", "Disco", "Western"], {"duration": 10})


def pushed(world: World, owner: User, public_id: str, *, ago: float = 0.0) -> int:
    """A battle pushed ``ago`` seconds ago."""
    poll_id = world.poll(owner, public_id, BATTLE, open_it=False)
    with world.database.write() as db:
        polls.open_poll(db, owner, poll_id, now=utcnow() - dt.timedelta(seconds=ago))
    return poll_id


def closed(world: World, owner: User, public_id: str, taps: Taps | None) -> int:
    poll_id = pushed(world, owner, public_id)
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id, taps_of=lambda _poll_id: taps)
    return poll_id


def world_open_poll(world: World, public_id: str) -> int:
    """The poll open in the session (one)."""
    with world.database.read() as db:
        assembly = assemblies.find_assembly(db, public_id)
        assert assembly is not None
        (poll_id,) = [p.id for p in assembly.polls if p.status.value == "open"]
        return poll_id


def battle_card(page: str) -> str:
    cards = re.findall(r'<section class="screen-result is-battle".*?</section>', page, re.DOTALL)
    assert len(cards) == 1, page
    return cards[0]


def view(world: World, public_id: str, now: dt.datetime) -> screen_service.ScreenView:
    with world.database.read() as db:
        assembly = assemblies.find_assembly(db, public_id)
        assert assembly is not None
        return screen_service.screen_view(db, world.keyring, assembly, now=now)


class TestView:
    def test_phases_on_the_stage_alone(self, world: World, owner: User, session_id: str) -> None:
        poll_action(world, owner, "close_poll", world.poll(owner, session_id))
        poll_id = pushed(world, owner, session_id)
        with world.database.read() as db:
            opened = db.get_one(Poll, poll_id).opened_at
        assert opened is not None
        expected = [
            (0.5, screen_service.BattlePhase.CHOOSE, CHOOSE_SECONDS),
            (CHOOSE_SECONDS + 0.5, screen_service.BattlePhase.TAPS, 10),
            (CHOOSE_SECONDS + 10, screen_service.BattlePhase.OVER, 0),
            (CHOOSE_SECONDS + 30, screen_service.BattlePhase.OVER, 0),  # not closed yet
        ]
        for seconds, phase, left in expected:
            seen = view(world, session_id, opened + dt.timedelta(seconds=seconds))
            assert seen.hero is screen_service.Part.BATTLE
            assert seen.side == []
            assert seen.battle is not None
            assert (seen.battle.poll.id, seen.battle.phase, seen.battle.left) == (
                poll_id,
                phase,
                left,
            )
            assert seen.open_polls == []
            assert poll_id not in [poll.id for poll in seen.past_polls]
        seen = view(world, session_id, opened)
        assert seen.battle is not None
        assert seen.battle.levels == [0, 0, 0] and seen.battle.totals == [0, 0, 0]
        assert (seen.battle.phones, seen.battle.peak) == (0, 1)

    def test_ranking_shown_once_closed(self, world: World, owner: User, session_id: str) -> None:
        poll_id = closed(world, owner, session_id, Taps({1: 3}, phones=1))
        seen = view(world, session_id, utcnow())
        assert seen.battle is None
        assert seen.hero is screen_service.Part.RESULT
        result = seen.stage_result
        assert result is not None and result.poll.id == poll_id
        assert result.battle and result.detail  # whatever the number of ballots (none)
        assert result.verdict is screen_service.Verdict.DECIDED


class TestStage:
    def test_choice_phase(self, client: TestClient, world: World, owner: User, url: str) -> None:
        session_id = url.split("/")[1]
        poll_id = pushed(world, owner, session_id)
        page = client.get(f"{url}/state").text
        strict_page(page)
        assert f'data-battle="{poll_id}"' in page
        assert 'class="screen-battle is-choose"' in page
        assert "data-battle-count>10<" in page or "data-battle-count>9<" in page
        assert "screen-open" not in page  # never a card of a vote in progress
        for label in ("Pirates", "Disco", "Western"):
            assert f'<p class="gauge-label"><span>{label}</span></p>' in page
        assert page.count("data-gauge") == 3
        assert "tone-3" in page
        for text in RESULT_TEXTS:
            assert text not in page, text

    def test_gauges_and_totals_as_the_arena_holds_them(
        self, app: FastAPI, client: TestClient, world: World, owner: User, url: str
    ) -> None:
        session_id = url.split("/")[1]
        poll_id = pushed(world, owner, session_id, ago=CHOOSE_SECONDS + 2)
        with world.database.read() as db:
            opened = db.get_one(Poll, poll_id).opened_at
        assert opened is not None
        arena = app.state.arena
        arena.start(poll_id, session_id, (1, 2, 3), opened.timestamp(), 10)
        assert arena.tap(poll_id, 1, 2, 12) == 12
        page = client.get(f"{url}/state").text
        strict_page(page)
        assert 'class="screen-battle is-taps"' in page
        tap_start = int((opened + dt.timedelta(seconds=CHOOSE_SECONDS)).timestamp() * 1000)
        assert f'data-tap-start="{tap_start}"' in page
        assert f'data-tap-end="{tap_start + 10_000}"' in page
        assert re.findall(r'data-level="(\d+)"', page) == ["0", "12", "0"]
        assert re.findall(r"data-total>(\d+)<", page) == ["0", "12", "0"]
        assert 'data-peak="12"' in page
        assert "data-phones" in page
        arena.end(poll_id)

    def test_other_blocks_wait(
        self, client: TestClient, world: World, owner: User, url: str
    ) -> None:
        session_id = url.split("/")[1]
        with world.database.write() as db:
            assembly = assemblies.find_assembly(db, session_id)
            assert assembly is not None
            assembly.agenda = [{"title": "Rapport moral"}]
        assert "screen-agenda" in client.get(f"{url}/state").text
        pushed(world, owner, session_id)
        page = client.get(f"{url}/state").text
        assert "screen-agenda" not in page
        assert "screen-join" not in page
        assert "has-side" not in page


class TestRanking:
    def test_winner_and_every_total(
        self, client: TestClient, world: World, owner: User, url: str
    ) -> None:
        session_id = url.split("/")[1]
        closed(world, owner, session_id, Taps({1: 30, 2: 75}, phones=3))
        card = battle_card(client.get(f"{url}/state").text)
        strict_page(card)
        assert 'data-verdict="decided"' in card
        assert re.search(r'data-verdict="decided">.*Disco', card, re.DOTALL)
        assert re.findall(r'data-battle-row="(\d)"', card) == ["2", "1", "3"]
        assert re.findall(r'data-taps="(\d+)"', card) == ["75", "30", "0"]
        assert card.count("is-win") == 1
        assert "screen-detail-off" not in card  # fewer than 10 ballots: still every total
        assert "battle-interrupted" not in card

    def test_tie_reported(self, client: TestClient, world: World, owner: User, url: str) -> None:
        session_id = url.split("/")[1]
        closed(world, owner, session_id, Taps({1: 5, 3: 5}, phones=2))
        card = battle_card(client.get(f"{url}/state").text)
        assert 'data-verdict="tie"' in card
        assert card.count('class="tag-tie"') == 2
        assert card.count("is-win") == 2

    def test_nobody_tapped_and_interrupted(
        self, client: TestClient, world: World, owner: User, url: str
    ) -> None:
        session_id = url.split("/")[1]
        closed(world, owner, session_id, None)  # the arena lost it: a restart
        card = battle_card(client.get(f"{url}/state").text)
        assert 'data-verdict="none"' in card
        assert "battle-interrupted" in card
        assert re.findall(r'<span class="tally-rank">([^<]*)</span>', card) == ["\u2013"] * 3

    def test_in_the_questions_put_to_the_vote(
        self, client: TestClient, world: World, owner: User, url: str
    ) -> None:
        session_id = url.split("/")[1]
        battle = closed(world, owner, session_id, Taps({2: 4}, phones=1))
        poll_action(world, owner, "show_on_screen", battle, shown=False)
        world.poll(owner, session_id, RESOLUTION)
        page = client.get(f"{url}/state").text
        line = re.search(rf'<li class="closed" data-past-poll="{battle}">.*?</li>', page, re.DOTALL)
        assert line is not None
        assert "Vote clos" not in line.group(0)
        assert "past-state" in line.group(0)
        poll_action(world, owner, "show_on_screen", battle, shown=True)
        later = world.poll(owner, session_id, RESOLUTION, open_it=False)
        with world.database.write() as db:
            polls.cancel_poll(db, owner, world_open_poll(world, session_id), "Doublon")
            polls.open_poll(db, owner, later)
            polls.close_poll(db, owner, later)
            polls.show_on_screen(db, owner, later, True)  # the latest result takes the stage
        page = client.get(f"{url}/state").text
        line = re.search(rf'data-past-poll="{battle}">.*?</li>', page, re.DOTALL)
        assert line is not None
        assert "past-verdict is-decided" in line.group(0)
        assert "Disco" in line.group(0)


class TestWhatTheRoomSees:
    @pytest.fixture
    def public_id(self, world: World, owner: User) -> str:
        return world.assembly(owner)

    def test_battle_line_and_buttons_wait(
        self, admin: TestClient, world: World, owner: User, public_id: str
    ) -> None:
        panel = admin.get(f"/manage/{public_id}/panel").text
        assert 'id="screen-toggle-timer"' in panel
        pushed(world, owner, public_id)
        panel = admin.get(f"/manage/{public_id}/panel").text
        assert "Thème de la fête" in panel
        assert 'id="screen-toggle-timer"' not in panel
        assert 'id="screen-toggle-join"' not in panel

    def test_ranking_line(
        self, admin: TestClient, world: World, owner: User, public_id: str
    ) -> None:
        battle = closed(world, owner, public_id, Taps({1: 2}, phones=1))
        panel = admin.get(f"/manage/{public_id}/panel").text
        assert f'id="screen-toggle-result-{battle}"' in panel
        assert "Pirates" in panel
