"""Chronometer of the projection screen, run from the session console.

The state lives on ``Assembly`` (``timer_*`` columns). The chronometer counts down from
``timer_duration`` seconds, or up when that is None; ``timer_elapsed`` holds the whole seconds
counted before the current run, which started at ``timer_started_at`` (None while paused or
reset). Readers compute the time shown from the server's clock (``state``), so every screen and
console agrees, and the scripts keep counting from the same values.

Arithmetic is exact: whole seconds in the database, ``timedelta`` (microseconds) in between.
Pausing keeps the whole seconds counted, rounded down, which never changes the time shown: a
countdown shows the seconds left rounded up, so ``D - floor(t) == ceil(D - t)``. The one
exception is a pause within the first second, which keeps one second: zero counted and stopped
means "not started" (``ready``), and the console then offers the setup form again.

Every function takes the current ``owner``: another account's session raises
``NotFoundError``, like a missing one. ``now`` is injectable for tests.

No audit entry: the chronometer has no effect on votes, and logging each start or pause of a
speaking time would drown the session log (FR-84).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy.orm import Session

from agaura.models import Assembly, AssemblyStatus, User, utcnow
from agaura.services import assemblies
from agaura.services.assemblies import clean_text, require_status
from agaura.services.errors import InvalidInputError, InvalidStateError

MAX_DURATION = 10 * 3600  # seconds
MAX_LABEL_LENGTH = 100
EXTRA_TIME = 60  # seconds added by ``add_time``
_SECOND = dt.timedelta(seconds=1)
_ZERO = dt.timedelta(0)


def clock(seconds: int) -> str:
    """``m:ss`` under an hour, ``h:mm:ss`` beyond (``static/js/{screen,timer}.js`` match)."""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


@dataclass(frozen=True, slots=True)
class TimerState:
    """The chronometer of a session at a given instant."""

    shown: bool  # on the projection screen
    label: str | None
    duration: int | None  # seconds; None: counts up
    base_elapsed: int  # whole seconds counted before the current run
    started_at: dt.datetime | None  # start of the current run; None while stopped
    elapsed: dt.timedelta  # counted so far, current run included

    @property
    def running(self) -> bool:
        return self.started_at is not None

    @property
    def counting_up(self) -> bool:
        return self.duration is None

    @property
    def paused(self) -> bool:
        """Stopped after counting some time (``ready`` when nothing was counted yet)."""
        return not self.running and self.base_elapsed > 0

    @property
    def ready(self) -> bool:
        return not self.running and self.base_elapsed == 0

    @property
    def overtime(self) -> bool:
        """A countdown that reached zero: it goes on counting the time past the end."""
        return self.duration is not None and self.elapsed >= dt.timedelta(seconds=self.duration)

    @property
    def seconds(self) -> int:
        """Whole seconds shown: counted (up), left rounded up (down), or past the end."""
        if self.duration is None:
            return self.elapsed // _SECOND
        left = dt.timedelta(seconds=self.duration) - self.elapsed
        if left > _ZERO:
            return -(-left // _SECOND)
        return -left // _SECOND

    @property
    def display(self) -> str:
        return ("+" if self.overtime else "") + clock(self.seconds)

    @property
    def duration_display(self) -> str | None:
        return None if self.duration is None else clock(self.duration)

    @property
    def started_ms(self) -> int | None:
        """Start of the current run in milliseconds since the epoch, for the scripts."""
        return None if self.started_at is None else int(self.started_at.timestamp() * 1000)


def state(assembly: Assembly, *, now: dt.datetime | None = None) -> TimerState:
    """The chronometer of ``assembly`` at ``now`` (a session already checked by the caller)."""
    now = now or utcnow()
    started_at = assembly.timer_started_at
    elapsed = dt.timedelta(seconds=assembly.timer_elapsed)
    if started_at is not None:
        elapsed += max(now - started_at, _ZERO)  # a clock set back never counts backwards
    return TimerState(
        shown=assembly.timer_shown,
        label=assembly.timer_label,
        duration=assembly.timer_duration,
        base_elapsed=assembly.timer_elapsed,
        started_at=started_at,
        elapsed=elapsed,
    )


def _owned(db: Session, owner: User, public_id: str) -> Assembly:
    """A session of ``owner`` that has not ended: the chronometer is not shown after it."""
    assembly = assemblies.get_assembly(db, owner, public_id)
    require_status(assembly, AssemblyStatus.PREPARATION, AssemblyStatus.IN_SESSION)
    return assembly


def _stop(assembly: Assembly, now: dt.datetime) -> None:
    """Add the whole seconds of the current run to the count, then stop. A run shorter than a
    second from zero counts one, so the chronometer stays paused rather than ``ready``."""
    if assembly.timer_started_at is not None:
        run = max(now - assembly.timer_started_at, _ZERO)
        counted = run // _SECOND
        if counted == 0 and run > _ZERO and assembly.timer_elapsed == 0:
            counted = 1
        assembly.timer_elapsed += counted
        assembly.timer_started_at = None


def start(
    db: Session,
    owner: User,
    public_id: str,
    *,
    duration: int | None,
    label: str | None = None,
    show: bool = True,
    now: dt.datetime | None = None,
) -> TimerState:
    """Start from zero: a countdown of ``duration`` seconds, or counting up when None.

    Replaces whatever the chronometer was doing. ``show`` puts it on the projection screen.
    """
    now = now or utcnow()
    if duration is not None and not 1 <= duration <= MAX_DURATION:
        raise InvalidInputError("timer_invalid_duration", "duration")
    clean_label = clean_text(label, max_length=MAX_LABEL_LENGTH, field="label", required=False)
    assembly = _owned(db, owner, public_id)
    assembly.timer_duration = duration
    assembly.timer_label = clean_label
    assembly.timer_elapsed = 0
    assembly.timer_started_at = now
    assembly.timer_shown = show
    return state(assembly, now=now)


def pause(
    db: Session, owner: User, public_id: str, *, now: dt.datetime | None = None
) -> TimerState:
    """Stop counting, keeping the time counted. Pausing a stopped chronometer changes nothing
    (a double tap, or two consoles)."""
    now = now or utcnow()
    assembly = _owned(db, owner, public_id)
    _stop(assembly, now)
    return state(assembly, now=now)


def resume(
    db: Session, owner: User, public_id: str, *, now: dt.datetime | None = None
) -> TimerState:
    """Count again from the time already counted (from zero after a reset). Resuming a running
    chronometer changes nothing."""
    now = now or utcnow()
    assembly = _owned(db, owner, public_id)
    if assembly.timer_started_at is None:
        assembly.timer_started_at = now
    return state(assembly, now=now)


def reset(
    db: Session, owner: User, public_id: str, *, now: dt.datetime | None = None
) -> TimerState:
    """Back to zero (a countdown to its full duration), stopped; label and display are kept."""
    now = now or utcnow()
    assembly = _owned(db, owner, public_id)
    assembly.timer_elapsed = 0
    assembly.timer_started_at = None
    return state(assembly, now=now)


def add_time(
    db: Session,
    owner: User,
    public_id: str,
    *,
    seconds: int = EXTRA_TIME,
    now: dt.datetime | None = None,
) -> TimerState:
    """Lengthen a countdown, running or not (a speaker granted one more minute)."""
    now = now or utcnow()
    assembly = _owned(db, owner, public_id)
    if assembly.timer_duration is None:
        raise InvalidStateError("timer_counting_up")
    if not 1 <= seconds <= MAX_DURATION - assembly.timer_duration:
        raise InvalidInputError("timer_invalid_duration", "duration")
    assembly.timer_duration += seconds
    return state(assembly, now=now)


def show(
    db: Session, owner: User, public_id: str, shown: bool, *, now: dt.datetime | None = None
) -> TimerState:
    """Show the chronometer on the projection screen, or hide it; it keeps counting."""
    now = now or utcnow()
    assembly = _owned(db, owner, public_id)
    assembly.timer_shown = shown
    return state(assembly, now=now)
