"""Aura battles under way (SPEC §6.8): the taps of each option, in memory only.

The application runs one process (SPEC §12.2), so the taps of a battle live here, in the
``Arena`` of the application (``app.state.arena``), from the push of the poll to its closing;
only the totals reach the database, when the poll closes (``services/polls.py``). The arena
keeps, for each battle:

- per option, the total of taps and those of the last ``WINDOW`` seconds (the gauge);
- per phone (its credential id, never its option), an allowance of one second of taps at the
  start of the tap phase, refilled at ``TAP_RATE`` taps a second up to ``CAPACITY``, and the set
  of phones that tapped, for their number.

A phone may send taps for any option of the battle: its allowance is the same whatever the
option, so switching gains nothing and the server never needs to know which option a phone
picked. Nothing here goes to a log or to the database.

Times are seconds since the epoch, like ``time.time()``. Every method is thread-safe: the tap
route and the ticker run on the event loop, pushes and closings in worker threads.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from agaura.voting.aura import CHOOSE_SECONDS, GRACE_SECONDS, Taps

TAP_RATE = 15  # taps a phone may count per second, about what two thumbs can do
CAPACITY = 2 * TAP_RATE  # an allowance saved up while a batch was delayed
MAX_BATCH = 99  # taps one request may carry (two digits, SPEC §9.2)
GRACE = float(GRACE_SECONDS)  # taps in flight still count this long after the end
WINDOW = 1.5  # the gauge: the taps of the last WINDOW seconds
TICK = 0.25  # the ticker sends the gauges four times a second
IDLE = 0.5  # how often the ticker looks for a battle when there is none
STALE = 60.0  # a battle still in the arena this long after its end is forgotten


@dataclass(slots=True)
class _Allowance:
    tokens: float
    at: float


@dataclass(slots=True)
class Battle:
    """One battle: its phases from the push, and the taps counted so far."""

    poll_id: int
    public_id: str
    options: tuple[int, ...]  # positions, in order
    tap_start: float
    tap_end: float
    # Started after its tap phase began (a restart of the server): taps may have been lost.
    interrupted: bool = False
    totals: dict[int, int] = field(default_factory=dict)
    final_sent: bool = False
    _recent: deque[tuple[float, int, int]] = field(default_factory=deque)
    _allowances: dict[int, _Allowance] = field(default_factory=dict)

    @property
    def phones(self) -> int:
        return len(self._allowances)

    def accepts(self, now: float) -> bool:
        """Taps count from the start of the tap phase to its end, plus the grace."""
        return self.tap_start <= now < self.tap_end + GRACE

    def due(self, now: float) -> bool:
        """Over, grace included: the poll may close with the totals."""
        return now >= self.tap_end + GRACE

    def tap(self, phone: int, option: int, count: int, now: float) -> int:
        """Count up to ``count`` taps of ``phone`` for ``option``; returns those counted."""
        if option not in self.options or count <= 0 or not self.accepts(now):
            return 0
        refill_until = min(now, self.tap_end)
        allowance = self._allowances.get(phone)
        if allowance is None:
            # One second of taps from the start: a phone that bursts at once keeps them all.
            allowance = self._allowances[phone] = _Allowance(float(TAP_RATE), self.tap_start)
        if refill_until > allowance.at:
            allowance.tokens = min(
                CAPACITY, allowance.tokens + (refill_until - allowance.at) * TAP_RATE
            )
            allowance.at = refill_until
        counted = min(count, MAX_BATCH, int(allowance.tokens))
        if counted <= 0:
            return 0
        allowance.tokens -= counted
        self.totals[option] = self.totals.get(option, 0) + counted
        self._recent.append((min(now, self.tap_end), option, counted))
        return counted

    def levels(self, now: float) -> dict[int, int]:
        """Taps of each option in the last ``WINDOW`` seconds, frozen at the end."""
        at = min(now, self.tap_end)
        while self._recent and self._recent[0][0] <= at - WINDOW:
            self._recent.popleft()
        levels = dict.fromkeys(self.options, 0)
        for _when, option, count in self._recent:
            levels[option] += count
        return levels

    def frame(self, now: float) -> dict[str, Any] | None:
        """What the projection screen shows now, or None: nothing before the tap phase, one
        frame per tick during it, then one last frame (``final``) frozen at its end."""
        if now < self.tap_start or self.final_sent:
            return None
        final = now >= self.tap_end
        self.final_sent = final
        levels = self.levels(now)
        return {
            "poll": self.poll_id,
            "levels": [levels[option] for option in self.options],
            "totals": [self.totals.get(option, 0) for option in self.options],
            "phones": self.phones,
            "final": final,
        }

    def result(self) -> Taps:
        return Taps(totals=dict(self.totals), phones=self.phones, interrupted=self.interrupted)


class Arena:
    """The battles under way in the process, and the phones they recognize."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._battles: dict[int, Battle] = {}
        # (session ID, digest of the device cookie) → credential id, while a battle of the
        # session runs: a tap then costs no database read. Emptied with the session's battles.
        self._phones: dict[tuple[str, str], int] = {}

    def start(
        self,
        poll_id: int,
        public_id: str,
        options: Iterable[int],
        opened_at: float,
        duration: int,
    ) -> Battle:
        """The battle of a poll just pushed, or found open (after a restart). Starting one again
        changes nothing. Started once its tap phase has begun, it says taps may be lost."""
        with self._lock:
            battle = self._battles.get(poll_id)
            if battle is None:
                tap_start = opened_at + CHOOSE_SECONDS
                battle = self._battles[poll_id] = Battle(
                    poll_id=poll_id,
                    public_id=public_id,
                    options=tuple(options),
                    tap_start=tap_start,
                    tap_end=tap_start + duration,
                    interrupted=self.clock() > tap_start,
                )
            return battle

    def battle(self, poll_id: int) -> Battle | None:
        with self._lock:
            return self._battles.get(poll_id)

    @property
    def busy(self) -> bool:
        return bool(self._battles)

    def tap(self, poll_id: int, phone: int, option: int, count: int) -> int:
        with self._lock:
            battle = self._battles.get(poll_id)
            return 0 if battle is None else battle.tap(phone, option, count, self.clock())

    def frames(self) -> list[tuple[str, dict[str, Any]]]:
        """(session ID, frame) of each battle with something to show now."""
        now = self.clock()
        with self._lock:
            found = [(battle.public_id, battle.frame(now)) for battle in self._battles.values()]
        return [(public_id, frame) for public_id, frame in found if frame is not None]

    def due(self) -> list[int]:
        """Polls whose battle is over, grace included."""
        now = self.clock()
        with self._lock:
            return [poll_id for poll_id, battle in self._battles.items() if battle.due(now)]

    def view(self, poll_id: int) -> dict[str, Any] | None:
        """The gauges and totals now, for a screen drawn during the battle."""
        now = self.clock()
        with self._lock:
            battle = self._battles.get(poll_id)
            if battle is None:
                return None
            levels = battle.levels(now)
            return {
                "levels": [levels[option] for option in battle.options],
                "totals": [battle.totals.get(option, 0) for option in battle.options],
                "phones": battle.phones,
            }

    def result(self, poll_id: int) -> Taps | None:
        """The taps of a battle, for its closing; None when the arena never saw it."""
        with self._lock:
            battle = self._battles.get(poll_id)
            return None if battle is None else battle.result()

    def end(self, poll_id: int) -> None:
        """Forget a battle once its poll is closed or cancelled, and the phones of its session
        when no other battle runs there."""
        with self._lock:
            battle = self._battles.pop(poll_id, None)
            if battle is None:
                return
            if not any(other.public_id == battle.public_id for other in self._battles.values()):
                for key in [key for key in self._phones if key[0] == battle.public_id]:
                    del self._phones[key]

    def polls_of(self, public_id: str) -> list[int]:
        """The polls of the battles of a session in the arena."""
        with self._lock:
            return [poll_id for poll_id, b in self._battles.items() if b.public_id == public_id]

    def prune(self) -> None:
        """Forget the battles over for ``STALE`` seconds whose poll nothing closed (a session
        deleted meanwhile)."""
        now = self.clock()
        with self._lock:
            stale = [p for p, battle in self._battles.items() if now >= battle.tap_end + STALE]
        for poll_id in stale:
            self.end(poll_id)

    def phone(self, public_id: str, digest: str) -> int | None:
        with self._lock:
            return self._phones.get((public_id, digest))

    def remember_phone(self, public_id: str, digest: str, credential_id: int) -> None:
        """Only while a battle of the session runs: ``end`` forgets the phones of its session."""
        with self._lock:
            if any(battle.public_id == public_id for battle in self._battles.values()):
                self._phones[(public_id, digest)] = credential_id
