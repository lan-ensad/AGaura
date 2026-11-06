"""Chronometer of the projection screen (FR-74): transitions, exact arithmetic, isolation."""

from __future__ import annotations

import datetime as dt

import pytest

from agaura.models import Assembly, User
from agaura.services import assemblies, timer
from agaura.services.errors import InvalidInputError, InvalidStateError, NotFoundError
from tests.conftest import World

T0 = dt.datetime(2026, 3, 14, 18, 0, tzinfo=dt.UTC)


def at(seconds: float) -> dt.datetime:
    return T0 + dt.timedelta(seconds=seconds)


@pytest.fixture
def public_id(world: World, owner: User) -> str:
    return world.assembly(owner)


def run(
    world: World, owner: User, public_id: str, action: str, **kwargs: object
) -> timer.TimerState:
    with world.database.write() as db:
        return getattr(timer, action)(db, owner, public_id, **kwargs)


def stored(world: World, public_id: str) -> Assembly:
    with world.database.read() as db:
        assembly = assemblies.find_assembly(db, public_id)
        assert assembly is not None
        return assembly


def shown_at(world: World, public_id: str, seconds: float) -> str:
    return timer.state(stored(world, public_id), now=at(seconds)).display


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0:00"),
        (5, "0:05"),
        (59, "0:59"),
        (60, "1:00"),
        (754, "12:34"),
        (3600, "1:00:00"),
        (36000, "10:00:00"),
        (3661, "1:01:01"),
    ],
)
def test_clock(seconds: int, text: str) -> None:
    assert timer.clock(seconds) == text


def test_default_state(world: World, public_id: str) -> None:
    state = timer.state(stored(world, public_id), now=T0)
    assert (state.shown, state.label, state.duration, state.running) == (False, None, None, False)
    assert state.ready and not state.paused and state.counting_up and not state.overtime
    assert (state.display, state.started_ms, state.duration_display) == ("0:00", None, None)


class TestCountdown:
    def test_counts_down_rounding_up(self, world: World, owner: User, public_id: str) -> None:
        state = run(
            world, owner, public_id, "start", duration=180, label=" Temps de parole ", now=T0
        )
        assert state.running and state.shown and state.label == "Temps de parole"
        assert state.started_ms == int(T0.timestamp() * 1000)
        assert (state.display, state.duration_display) == ("3:00", "3:00")
        # 0.4 s counted: 179.6 s left, shown as 3:00 until a whole second has gone.
        assert shown_at(world, public_id, 0.4) == "3:00"
        assert shown_at(world, public_id, 1) == "2:59"
        assert shown_at(world, public_id, 179.001) == "0:01"

    def test_overtime(self, world: World, owner: User, public_id: str) -> None:
        run(world, owner, public_id, "start", duration=60, now=T0)
        state = timer.state(stored(world, public_id), now=at(60))
        assert state.overtime and state.display == "+0:00"
        state = timer.state(stored(world, public_id), now=at(75.9))
        assert (state.overtime, state.seconds, state.display) == (True, 15, "+0:15")
        assert shown_at(world, public_id, 60 + 3600) == "+1:00:00"

    def test_pause_keeps_the_time_shown(self, world: World, owner: User, public_id: str) -> None:
        run(world, owner, public_id, "start", duration=60, now=T0)
        assert shown_at(world, public_id, 10.7) == "0:50"
        state = run(world, owner, public_id, "pause", now=at(10.7))
        # The whole seconds are kept (10): 50 s left, exactly what was shown.
        assert (state.running, state.paused, state.display) == (False, True, "0:50")
        assert stored(world, public_id).timer_elapsed == 10
        assert stored(world, public_id).timer_started_at is None
        assert shown_at(world, public_id, 500) == "0:50"  # paused: time does not pass

        run(world, owner, public_id, "resume", now=at(100))
        assert shown_at(world, public_id, 100) == "0:50"
        assert shown_at(world, public_id, 130) == "0:20"
        run(world, owner, public_id, "pause", now=at(130.2))
        run(world, owner, public_id, "resume", now=at(200))
        assert stored(world, public_id).timer_elapsed == 40
        assert shown_at(world, public_id, 225) == "+0:05"

    def test_pause_within_the_first_second_stays_paused(
        self, world: World, owner: User, public_id: str
    ) -> None:
        run(world, owner, public_id, "start", duration=60, now=T0)
        state = run(world, owner, public_id, "pause", now=at(0.3))
        assert (state.paused, state.ready, state.display) == (True, False, "0:59")
        run(world, owner, public_id, "resume", now=at(5))
        run(world, owner, public_id, "pause", now=at(5.4))  # not from zero: rounded down
        assert stored(world, public_id).timer_elapsed == 1

    def test_pause_at_the_very_start_changes_nothing(
        self, world: World, owner: User, public_id: str
    ) -> None:
        run(world, owner, public_id, "start", duration=60, now=T0)
        assert run(world, owner, public_id, "pause", now=T0).ready

    def test_pause_and_resume_are_idempotent(
        self, world: World, owner: User, public_id: str
    ) -> None:
        run(world, owner, public_id, "start", duration=60, now=T0)
        run(world, owner, public_id, "resume", now=at(5))  # already running: unchanged
        assert stored(world, public_id).timer_started_at == T0
        run(world, owner, public_id, "pause", now=at(10))
        run(world, owner, public_id, "pause", now=at(20))  # already paused: unchanged
        assert stored(world, public_id).timer_elapsed == 10

    def test_reset(self, world: World, owner: User, public_id: str) -> None:
        run(world, owner, public_id, "start", duration=120, label="Débat", now=T0)
        state = run(world, owner, public_id, "reset", now=at(30))
        assert (state.ready, state.running, state.display) == (True, False, "2:00")
        assert (state.label, state.shown, state.duration) == ("Débat", True, 120)
        state = run(world, owner, public_id, "resume", now=at(40))  # starts the same duration
        assert state.running and shown_at(world, public_id, 41) == "1:59"

    def test_add_time(self, world: World, owner: User, public_id: str) -> None:
        run(world, owner, public_id, "start", duration=60, now=T0)
        assert shown_at(world, public_id, 70) == "+0:10"
        state = run(world, owner, public_id, "add_time", now=at(70))
        assert (state.duration, state.overtime, state.display) == (120, False, "0:50")
        run(world, owner, public_id, "add_time", seconds=30, now=at(70))
        assert stored(world, public_id).timer_duration == 150

    def test_start_replaces_whatever_ran(self, world: World, owner: User, public_id: str) -> None:
        run(world, owner, public_id, "start", duration=60, label="Avant", now=T0)
        run(world, owner, public_id, "pause", now=at(30))
        state = run(world, owner, public_id, "start", duration=300, show=False, now=at(40))
        assert (state.label, state.duration, state.base_elapsed) == (None, 300, 0)
        assert state.running and not state.shown and state.display == "5:00"


class TestCountUp:
    def test_counts_whole_seconds(self, world: World, owner: User, public_id: str) -> None:
        state = run(world, owner, public_id, "start", duration=None, now=T0)
        assert state.counting_up and state.running and state.display == "0:00"
        assert shown_at(world, public_id, 0.99) == "0:00"
        assert shown_at(world, public_id, 75.5) == "1:15"
        run(world, owner, public_id, "pause", now=at(75.5))
        assert shown_at(world, public_id, 999) == "1:15"
        assert not timer.state(stored(world, public_id), now=at(10**6)).overtime

    def test_cannot_add_time(self, world: World, owner: User, public_id: str) -> None:
        run(world, owner, public_id, "start", duration=None, now=T0)
        with pytest.raises(InvalidStateError) as error:
            run(world, owner, public_id, "add_time", now=at(1))
        assert error.value.code == "timer_counting_up"


def test_show_and_hide(world: World, owner: User, public_id: str) -> None:
    state = run(world, owner, public_id, "show", shown=True, now=T0)
    assert state.shown and state.ready  # shown without running: the screen shows 0:00
    run(world, owner, public_id, "start", duration=60, show=True, now=T0)
    state = run(world, owner, public_id, "show", shown=False, now=at(10))
    assert not state.shown and state.running  # hidden, still counting
    assert shown_at(world, public_id, 20) == "0:40"


def test_clock_set_back_never_counts_backwards(world: World, owner: User, public_id: str) -> None:
    run(world, owner, public_id, "start", duration=60, now=T0)
    assert shown_at(world, public_id, -30) == "1:00"
    run(world, owner, public_id, "pause", now=at(-30))
    assert stored(world, public_id).timer_elapsed == 0


@pytest.mark.parametrize("duration", [0, -5, timer.MAX_DURATION + 1])
def test_invalid_duration(world: World, owner: User, public_id: str, duration: int) -> None:
    with pytest.raises(InvalidInputError) as error:
        run(world, owner, public_id, "start", duration=duration, now=T0)
    assert error.value.code == "timer_invalid_duration"
    assert stored(world, public_id).timer_started_at is None


def test_add_time_is_capped(world: World, owner: User, public_id: str) -> None:
    run(world, owner, public_id, "start", duration=timer.MAX_DURATION, now=T0)
    with pytest.raises(InvalidInputError):
        run(world, owner, public_id, "add_time", now=at(1))
    with pytest.raises(InvalidInputError):
        run(world, owner, public_id, "add_time", seconds=0, now=at(1))
    assert stored(world, public_id).timer_duration == timer.MAX_DURATION


def test_label_too_long(world: World, owner: User, public_id: str) -> None:
    run(world, owner, public_id, "start", duration=60, label="x" * 100, now=T0)
    with pytest.raises(InvalidInputError) as error:
        run(world, owner, public_id, "start", duration=60, label="x" * 101, now=T0)
    assert (error.value.code, error.value.detail) == ("too_long", "label")


def test_preparation_allowed_closed_refused(world: World, owner: User) -> None:
    public_id = world.assembly(owner, in_session=False)
    assert run(world, owner, public_id, "start", duration=600, now=T0).running
    with world.database.write() as db:
        assemblies.open_assembly(db, owner, public_id)
        assemblies.close_assembly(db, owner, public_id)
    for action, kwargs in [
        ("start", {"duration": 60}),
        ("pause", {}),
        ("resume", {}),
        ("reset", {}),
        ("add_time", {}),
        ("show", {"shown": True}),
    ]:
        with pytest.raises(InvalidStateError) as error:
            run(world, owner, public_id, action, now=T0, **kwargs)
        assert error.value.code == "assembly_closed", action


def test_another_account_cannot_touch_it(world: World, owner: User, public_id: str) -> None:
    intruder = world.user("intruder@example.org")
    for action, kwargs in [
        ("start", {"duration": 60}),
        ("pause", {}),
        ("resume", {}),
        ("reset", {}),
        ("add_time", {}),
        ("show", {"shown": True}),
    ]:
        with pytest.raises(NotFoundError):
            run(world, intruder, public_id, action, now=T0, **kwargs)
    assembly = stored(world, public_id)
    assert (assembly.timer_started_at, assembly.timer_shown) == (None, False)


def test_no_audit_entry(world: World, owner: User, public_id: str) -> None:
    """The chronometer has no effect on votes: it stays out of the session log."""
    from sqlalchemy import func, select

    from agaura.models import AuditEntry

    with world.database.read() as db:
        before = db.scalar(select(func.count()).select_from(AuditEntry))
    run(world, owner, public_id, "start", duration=60, now=T0)
    run(world, owner, public_id, "pause", now=at(1))
    run(world, owner, public_id, "show", shown=False, now=at(2))
    with world.database.read() as db:
        assert db.scalar(select(func.count()).select_from(AuditEntry)) == before
