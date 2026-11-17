"""The aura battle in the console (SPEC §6.8): open (no turnout, stopped early with its taps),
closed (its ranking, shown on the screen by itself, no number, no export), left out of the
summary; and the result macro of a battle."""

from __future__ import annotations

from html import unescape

from fastapi.testclient import TestClient

from agaura.models import Poll, User
from agaura.services import assemblies, polls
from agaura.services.polls import PollInput
from agaura.voting.aura import Taps
from agaura.web.messages import error_message
from agaura.web.templating import templates
from tests.conftest import RESOLUTION, World
from tests.web.conftest import post

BATTLE = PollInput("Thème de la fête", "aura", ["Pirates", "Disco", "Western"], {"duration": 5})

MACRO = templates.env.from_string(
    '{% from "macros/results.html" import result_block, verdict %}'
    "{{ verdict(poll, poll.result.data.outcome, option_labels(poll)) }}"
    "{{ result_block(poll, poll.result.data, show_turnout=true) }}"
)


def closed_battle(world: World, owner: User, public_id: str, taps: Taps) -> int:
    poll_id = world.poll(owner, public_id, BATTLE)
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id, taps_of=lambda _poll_id: taps)
    return poll_id


def tile(page: str, element_id: str) -> str:
    start = page.index(f'id="{element_id}"')
    end = page.find("</details>", start)
    return unescape(page[start:end])


def render(world: World, owner: User, poll_id: int) -> str:
    with world.database.read() as db:
        poll = polls.get_poll(db, owner, poll_id)
        _ = poll.options, poll.result
        return unescape(MACRO.render(poll=poll))


def test_an_open_battle(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, BATTLE)
    page = admin.get(f"/manage/{public_id}").text
    open_tile = tile(page, f"open-poll-{poll_id}")
    assert "#i-zap" in open_tile  # the pill of an animation
    assert "<progress" not in open_tile  # no ballot to count
    assert f"/polls/{poll_id}/close" in open_tile
    status = page[page.index('class="status-row"') :]
    assert "<dt>Bulletins</dt>" not in status[: status.index("</dl>")]
    panel = admin.get(f"/manage/{public_id}/panel").text
    assert f"/polls/{poll_id}/close" in panel and "<progress" not in panel.split("open-polls")[1]


def test_stopped_early_keeps_the_taps(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, BATTLE)
    arena = admin.app.state.arena  # type: ignore[attr-defined]
    with world.database.read() as db:
        poll = db.get_one(Poll, poll_id)
        assert poll.opened_at is not None
        opened = poll.opened_at.timestamp()
    arena.start(poll_id, public_id, (1, 2, 3), opened, 5)
    battle = arena.battle(poll_id)
    battle.tap(1, 2, 7, battle.tap_start + 1)
    response = post(admin, f"/manage/{public_id}/polls/{poll_id}/close")
    assert response.status_code == 303
    with world.database.read() as db:
        result = db.get_one(Poll, poll_id).result
        assert result is not None
        assert result.data["outcome"]["winners"] == [2]
    assert arena.battle(poll_id) is None  # forgotten once closed


def test_a_closed_battle(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    first = world.poll(owner, public_id)
    with world.database.write() as db:
        polls.close_poll(db, owner, first)
    poll_id = closed_battle(world, owner, public_id, Taps({1: 12, 2: 40}, phones=4))
    page = admin.get(f"/manage/{public_id}").text
    finished = tile(page, f"poll-{poll_id}")
    assert "data-on-screen" in finished  # shown by itself (FR-75)
    assert "N° " not in finished  # no number
    assert "ballots.csv" not in finished and f"#exports-{poll_id}" not in finished
    assert "Disco" in finished and "40" in finished
    assert "Bulletins exprimés" not in finished  # no ballot counts
    # The "Vote clos" command waits for a poll neither published nor shown: never a battle.
    assert 'id="just-closed"' not in page
    for name in ("ballots.csv", "export.json"):
        assert admin.get(f"/manage/{public_id}/polls/{poll_id}/{name}").status_code == 409
    assert f'id="exports-{poll_id}"' not in page
    with world.database.write() as db:
        assemblies.close_assembly(db, owner, public_id)
    summary = admin.get(f"/manage/{public_id}").text
    summary = summary[summary.index('id="summary"') :]
    summary = summary[: summary.index("</section>")]
    assert "<dt>Votes tenus</dt><dd>1</dd>" in summary


def test_the_screen_button_still_works(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    poll_id = closed_battle(world, owner, public_id, Taps({1: 3}, phones=1))
    response = post(admin, f"/manage/{public_id}/polls/{poll_id}/screen", {"shown": "0"})
    assert response.status_code == 303
    with world.database.read() as db:
        assert not db.get_one(Poll, poll_id).shown_on_screen


class TestResultMacro:
    def test_winner(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        poll_id = closed_battle(world, owner, public_id, Taps({1: 12, 2: 36}, phones=4))
        text = render(world, owner, poll_id)
        assert 'class="verdict success"' in text and "Disco" in text
        assert '<progress class="bar win" max="48" value="36">' in text
        assert "75,0 %" in text and "25,0 %" in text
        assert "alert-warning" not in text
        assert "kpis" not in text  # no ballot counts

    def test_tie(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        poll_id = closed_battle(world, owner, public_id, Taps({1: 9, 3: 9}, phones=2))
        text = render(world, owner, poll_id)
        assert 'class="verdict warning"' in text
        verdict = text[text.index("verdict warning") : text.index("</p>", text.index("verdict"))]
        assert "Pirates" in verdict and "Western" in verdict
        assert text.count('class="bar win"') == 2

    def test_interrupted_nobody_tapped(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, BATTLE)
        with world.database.write() as db:
            polls.close_poll(db, owner, poll_id)  # the arena never saw it: interrupted
        text = render(world, owner, poll_id)
        assert 'class="verdict warning"' in text
        assert "alert alert-warning" in text
        assert 'class="bar win"' not in text


def test_a_battle_waits_for_the_stage(admin: TestClient, world: World, owner: User) -> None:
    """Pushed while a poll is open, a battle is refused with its message; and the other way."""
    public_id = world.assembly(owner)
    world.poll(owner, public_id, RESOLUTION)
    battle = world.poll(owner, public_id, BATTLE, open_it=False)
    response = post(admin, f"/manage/{public_id}/polls/{battle}/open", follow_redirects=True)
    assert error_message("aura_needs_stage") in unescape(response.text)
    with world.database.read() as db:
        assert db.get_one(Poll, battle).status.value == "draft"
