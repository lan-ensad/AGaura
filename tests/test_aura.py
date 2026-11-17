"""The battles under way in memory (``agaura.aura``): phases, allowances, gauges, frames."""

from __future__ import annotations

import pytest

from agaura.aura import CAPACITY, GRACE, MAX_BATCH, STALE, TAP_RATE, WINDOW, Arena
from agaura.voting.aura import CHOOSE_SECONDS

OPENED = 1_000.0
START = OPENED + CHOOSE_SECONDS  # the tap phase
DURATION = 5
END = START + DURATION


class Clock:
    def __init__(self, now: float = OPENED) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def arena(clock: Clock) -> Arena:
    arena = Arena(clock)
    arena.start(7, "ABC123", (1, 2, 3), OPENED, DURATION)
    return arena


def test_start_once(arena: Arena) -> None:
    battle = arena.battle(7)
    assert battle is not None
    assert (battle.tap_start, battle.tap_end, battle.interrupted) == (START, END, False)
    assert arena.start(7, "ABC123", (1, 2), OPENED, 20) is battle
    assert arena.busy
    assert arena.polls_of("ABC123") == [7]
    assert arena.polls_of("OTHER1") == []


def test_started_late_says_taps_may_be_lost(clock: Clock) -> None:
    arena = Arena(clock)
    clock.now = START + 1
    assert arena.start(7, "ABC123", (1, 2), OPENED, DURATION).interrupted


def test_no_tap_before_the_tap_phase(arena: Arena, clock: Clock) -> None:
    clock.now = START - 0.1
    assert arena.tap(7, phone=1, option=1, count=5) == 0
    assert arena.frames() == []


def test_allowance(arena: Arena, clock: Clock) -> None:
    """At most TAP_RATE taps a second per phone, whatever the option, saved up to CAPACITY."""
    clock.now = START + 0.5  # one second of taps from the start, half a second refilled
    assert arena.tap(7, 1, 1, 50) == TAP_RATE + TAP_RATE // 2
    assert arena.tap(7, 1, 2, 50) == 0  # another option: the same allowance
    clock.now = START + 0.5 + CAPACITY / TAP_RATE + 1  # saved up for a long while
    assert arena.tap(7, 1, 3, 99) == CAPACITY
    assert arena.tap(7, 2, 1, 3) == 3  # another phone has its own
    battle = arena.battle(7)
    assert battle is not None
    assert battle.totals == {1: TAP_RATE + TAP_RATE // 2 + 3, 3: CAPACITY}
    assert battle.phones == 2


def test_batch_and_option_limits(arena: Arena, clock: Clock) -> None:
    clock.now = END  # allowance full
    assert MAX_BATCH >= CAPACITY
    assert arena.tap(7, 1, 9, 5) == 0  # unknown option
    assert arena.tap(7, 1, 1, 0) == 0
    assert arena.tap(8, 1, 1, 5) == 0  # unknown battle
    assert arena.tap(7, 1, 1, 5) == 5


def test_grace_then_over(arena: Arena, clock: Clock) -> None:
    clock.now = END + GRACE / 2  # in flight: still counted, from what was saved up to the end
    assert arena.tap(7, 1, 1, 99) == min(CAPACITY, DURATION * TAP_RATE)
    assert arena.due() == []
    clock.now = END + GRACE
    assert arena.tap(7, 2, 1, 5) == 0
    assert arena.due() == [7]


def test_gauges_rise_and_fall_then_freeze(arena: Arena, clock: Clock) -> None:
    clock.now = START + 1
    arena.tap(7, 1, 1, 10)
    arena.tap(7, 2, 2, 4)
    ((public_id, frame),) = arena.frames()
    assert public_id == "ABC123"
    assert frame == {
        "poll": 7,
        "levels": [10, 4, 0],
        "totals": [10, 4, 0],
        "phones": 2,
        "final": False,
    }
    clock.now = START + 1 + WINDOW + 0.1  # the room stopped tapping: the gauges fall
    assert arena.frames()[0][1]["levels"] == [0, 0, 0]
    clock.now = END - 0.5
    arena.tap(7, 1, 1, 6)
    clock.now = END + 0.4  # past the end: one last frame, frozen at the end
    ((_, last),) = arena.frames()
    assert last["final"] is True
    assert last["levels"] == [6, 0, 0]
    assert last["totals"] == [16, 4, 0]
    assert arena.frames() == []  # nothing more moves


def test_view_and_result(arena: Arena, clock: Clock) -> None:
    clock.now = START + 1
    arena.tap(7, 1, 2, 3)
    assert arena.view(7) == {"levels": [0, 3, 0], "totals": [0, 3, 0], "phones": 1}
    assert arena.view(8) is None
    result = arena.result(7)
    assert result is not None
    assert (dict(result.totals), result.phones, result.interrupted) == ({2: 3}, 1, False)
    assert arena.result(8) is None


def test_phones_known_while_a_battle_of_the_session_runs(arena: Arena) -> None:
    arena.remember_phone("ABC123", "digest", 42)
    arena.remember_phone("OTHER1", "digest", 43)  # no battle there: not kept
    assert arena.phone("ABC123", "digest") == 42
    assert arena.phone("OTHER1", "digest") is None
    arena.start(8, "ABC123", (1, 2), OPENED, DURATION)
    arena.end(7)
    assert arena.phone("ABC123", "digest") == 42  # battle 8 still runs there
    arena.end(8)
    arena.end(8)  # twice: nothing to do
    assert arena.phone("ABC123", "digest") is None
    assert not arena.busy


def test_prune_forgets_stale_battles(arena: Arena, clock: Clock) -> None:
    clock.now = END + STALE - 1
    arena.prune()
    assert arena.battle(7) is not None
    clock.now = END + STALE
    arena.prune()
    assert arena.battle(7) is None
