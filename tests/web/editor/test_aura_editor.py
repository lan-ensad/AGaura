"""The aura battle in the poll editor (SPEC §6.8): its card set apart at step 1, its form at
step 2 (options, duration of the taps, nothing else), saved and launched."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agaura.models import PollStatus, User
from agaura.services.polls import PollInput
from agaura.voting.aura import CHOOSE_SECONDS, DEFAULT_DURATION
from agaura.web.manage.editor import (
    EditorFields,
    fields_from_kind,
    fields_from_poll,
    rule_sentence,
    rule_texts,
    switch_kind,
)
from tests.conftest import World
from tests.web.conftest import post
from tests.web.editor.conftest import polls_of
from tests.web.test_icons import assert_icons_sound

BATTLE = PollInput("Thème de la fête", "aura", ["Pirates", "Disco"], {"duration": 15})


def new_url(public_id: str) -> str:
    return f"/manage/{public_id}/polls/new"


def battle_form(**changes: Any) -> dict[str, Any]:
    fields = fields_from_kind("aura").model_dump(exclude=set(EditorFields.BUTTONS))
    return {
        **fields,
        "title": "Thème de la fête",
        "option_label": ["Pirates", "Disco", "Western", ""],
        "option_description": ["", "", "", ""],
        "action": "draft",
        **changes,
    }


def test_step_one_sets_the_battle_apart(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    page = admin.get(new_url(public_id)).text
    vote, animation = page.split('data-group="animation"')
    assert "?kind=aura" not in vote
    assert f'href="{new_url(public_id)}?kind=aura"' in animation
    # No heading nor hint: the colour of its icon tells it apart (is-apart).
    assert "method-group-title" not in page and "group-animation-hint" not in page
    assert 'class="method-cards is-apart"' in page
    assert "#i-zap" in animation
    assert_icons_sound(page)


def test_step_two_shows_the_battle_settings_only(
    admin: TestClient, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    page = admin.get(new_url(public_id), params={"kind": "aura"}).text
    assert 'data-kind="aura"' in page
    assert page.count('name="duration"') == 4
    assert re.search(r'name="duration" value="5" id="duration-5" checked', page)
    assert 'data-max="6"' in page
    # No rule to edit, no blank vote, no automatic closing: it closes by itself.
    assert 'id="rule-edit"' not in page
    assert 'name="minutes"' not in page
    assert 'class="is-blank"' not in page
    assert 'name="action" value="launch"' in page  # in session
    assert_icons_sound(page)


def test_rule_sentence() -> None:
    fields = fields_from_kind("aura")
    assert fields.duration == str(DEFAULT_DURATION)
    assert rule_sentence(fields) == rule_texts()["aura"]


def test_save_a_draft(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    response = post(admin, new_url(public_id), battle_form(duration="20"))
    assert response.status_code == 303
    (poll,) = polls_of(world)
    assert (poll.method, poll.params, poll.status) == ("aura", {"duration": 20}, PollStatus.DRAFT)
    assert [option.label for option in poll.options] == ["Pirates", "Disco", "Western"]
    assert poll.blank_label is None
    edited = fields_from_poll(poll)
    assert (edited.kind, edited.duration) == ("aura", "20")
    page = admin.get(f"/manage/{public_id}/polls/{poll.id}/edit").text
    assert re.search(r'name="duration" value="20" id="duration-20" checked', page)


@pytest.mark.parametrize("duration", ["7", "30", "cinq"])
def test_other_durations_refused(
    admin: TestClient, world: World, owner: User, duration: str
) -> None:
    public_id = world.assembly(owner)
    response = post(admin, new_url(public_id), battle_form(duration=duration))
    assert response.status_code == 400
    assert 'id="error-duration"' in response.text
    assert polls_of(world) == []


def test_seven_options_refused_next_to_the_options(
    admin: TestClient, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    labels = [f"Option {n}" for n in range(1, 8)]
    response = post(
        admin, new_url(public_id), battle_form(option_label=labels, option_description=[""] * 7)
    )
    assert response.status_code == 400
    assert 'id="error-options"' in response.text
    # The seven rows typed stay, for the error to name them; no empty row is added.
    assert response.text.count('name="option_label"') == 7 + 1  # and the row template
    assert polls_of(world) == []


def test_launch_starts_the_battle(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    # A timer typed in another mode is never read for a battle.
    response = post(admin, new_url(public_id), battle_form(action="launch", minutes="3"))
    assert response.status_code == 303
    (poll,) = polls_of(world)
    assert poll.status is PollStatus.OPEN
    assert poll.opened_at is not None and poll.closes_at is not None
    assert poll.closes_at - poll.opened_at == dt.timedelta(
        seconds=CHOOSE_SECONDS + DEFAULT_DURATION
    )
    assert admin.app.state.arena.battle(poll.id) is not None  # type: ignore[attr-defined]


def test_launch_refused_while_a_poll_is_open(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    world.poll(owner, public_id)  # a resolution, open
    response = post(admin, new_url(public_id), battle_form(action="launch"))
    assert response.status_code == 400
    assert 'id="editor-errors"' in response.text
    assert [poll.method for poll in polls_of(world)] == ["single_choice"]


def test_switching_keeps_the_options() -> None:
    fields = EditorFields(
        kind="single_choice",
        title="Lieu",
        option_label=["Salle", "Parc", ""],
        option_description=["", "", ""],
        minutes="5",
    )
    battle = switch_kind(fields, "aura")
    assert (battle.kind, battle.title, battle.option_label) == ("aura", "Lieu", fields.option_label)
    assert battle.duration == str(DEFAULT_DURATION)
    back = switch_kind(battle, "approval")
    assert (back.kind, back.option_label) == ("approval", fields.option_label)


def test_draft_tile_and_push(admin: TestClient, world: World, owner: User) -> None:
    """A battle draft: its mode and the pill "Animation"; no "push with a timer"."""
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, BATTLE, open_it=False)
    page = admin.get(f"/manage/{public_id}").text
    tile = page[page.index(f'id="draft-{poll_id}"') :]
    tile = tile[: tile.index("</details>\n    </li>")]
    assert "#i-zap" in tile
    assert 'name="minutes"' not in tile
    response = post(admin, f"/manage/{public_id}/polls/{poll_id}/open")
    assert response.status_code == 303
    (poll,) = polls_of(world)
    assert poll.status is PollStatus.OPEN
