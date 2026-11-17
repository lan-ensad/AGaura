"""Read model of the projection screen (FR-70 to FR-75, FR-27), and the blocks the tribune hides.

The screen is reached through a secret link, without an account: everything here works from
the session returned by ``access.check_link_token`` and shows only what the whole room may see:
how to join while entries are open, the status of entries, attendees and quorum, the
chronometer, the agenda, and the questions put to the vote with the turnout of open ones.

**What the tribune hides** ("Ce que la salle voit", FR-70): the agenda and the way to join, each
with ``show_part``; the chronometer (``timer.show``) and each result (``polls.show_on_screen``)
have their own switch. The way to join hidden, the screen does not even receive the password,
and gives the address at its foot, as when entries are locked.

**A result only when the tribune shows it** (FR-75): a closed poll whose ``shown_on_screen``
is on, whether or not it is published to voters (FR-36). Never the result of an open poll (D4)
or of a cancelled one (FR-72); the count of each option only from ``SCREEN_DETAIL_MIN``
ballots, the verdict alone below; never anything about one voter. Drafts never appear.

**An aura battle** (SPEC §6.8), the exception: a game with no voting value, whose gauges and
totals of taps the whole room watches while it runs (``ScreenBattle``, on the stage alone,
from its push until its closing, frozen past its end); once closed, its ranking is shown by
itself (``polls._close``), with the taps of every option whatever their number.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from agaura.models import (
    AccessMode,
    Assembly,
    AssemblyStatus,
    Participation,
    Poll,
    PollStatus,
    User,
    utcnow,
)
from agaura.security import Keyring
from agaura.services import access, assemblies, audit, timer
from agaura.services.assemblies import require_status
from agaura.services.ballots import is_accepting_votes
from agaura.services.errors import InvalidInputError
from agaura.services.timer import TimerState
from agaura.voting import aura

_EPOCH = dt.datetime.min.replace(tzinfo=dt.UTC)

# Ballots from which the screen shows the count of each option (FR-75): below, a handful of
# ballots could tell the room how its few voters voted, so the screen gives the verdict alone.
SCREEN_DETAIL_MIN = 10


class Entries(StrEnum):
    """Whether the session accepts new people, as announced on the screen."""

    SOON = "soon"  # in preparation: phones can scan ahead and join at the opening
    OPEN = "open"
    LOCKED = "locked"  # FR-27
    ENDED = "ended"  # the session is closed


class Part(StrEnum):
    """Blocks of the screen; the layout gives the stage to one of them (``ScreenView.hero``)."""

    BATTLE = "battle"  # an aura battle under way: the stage to itself (SPEC §6.8)
    QUESTIONS = "questions"
    RESULT = "result"  # the latest result shown by the tribune (FR-75)
    JOIN = "join"
    TIMER = "timer"
    AGENDA = "agenda"
    MESSAGE = "message"


@dataclass(frozen=True, slots=True)
class ScreenPoll:
    """An open poll with the number of ballots cast so far (never what they contain)."""

    poll: Poll
    turnout: int


class Verdict(StrEnum):
    """What a result says, in one word for its colour; the words come from the templates."""

    ADOPTED = "adopted"
    REJECTED = "rejected"
    DECIDED = "decided"  # the option chosen, the people elected, the winner of a ranking
    TIE = "tie"  # reported, never broken (D6)
    NONE = "none"  # no option reaches the threshold, seats unfilled, no valid vote


def verdict(poll: Poll, outcome: Mapping[str, Any]) -> Verdict:
    """The verdict of a stored result, as ``macros/results.html`` states it to voters."""
    status = outcome["status"]
    if (
        poll.method == "single_choice"
        and "adopt_option" in poll.params
        and status != "no_valid_votes"
    ):
        adopted = outcome.get("adopted")
        if adopted is None:  # the option to adopt is tied at the top (plurality)
            return Verdict.TIE
        return Verdict.ADOPTED if adopted else Verdict.REJECTED
    if status == "decided":
        return Verdict.DECIDED
    if status == "tie":
        return Verdict.TIE
    return Verdict.NONE


@dataclass(frozen=True, slots=True)
class ScreenResult:
    """A closed poll whose result the tribune shows on the screen (FR-75)."""

    poll: Poll
    data: dict[str, Any]  # ``poll.result.data``

    @property
    def ballots(self) -> int:
        return int(self.data["counts"]["cast"])

    @property
    def battle(self) -> bool:
        """The ranking of an aura battle (SPEC §6.8), not the result of a vote."""
        return self.poll.is_aura

    @property
    def detail(self) -> bool:
        """The count of each option, from ``SCREEN_DETAIL_MIN`` ballots; the verdict alone
        below (FR-75). A battle always gives its taps: the room saw them grow."""
        return self.battle or self.ballots >= SCREEN_DETAIL_MIN

    @property
    def detail_min(self) -> int:
        return SCREEN_DETAIL_MIN

    @property
    def verdict(self) -> Verdict:
        return verdict(self.poll, self.data["outcome"])


class BattlePhase(StrEnum):
    """Where an aura battle stands, as the screen draws it; screen.js moves on by itself."""

    CHOOSE = "choose"  # the room picks an option on the phones, a countdown on the screen
    TAPS = "taps"  # the gauges move
    OVER = "over"  # frozen, until the poll closes and its ranking takes the stage


@dataclass(frozen=True, slots=True)
class ScreenBattle:
    """An aura battle under way (SPEC §6.8), with its gauges and totals when the arena of the
    process knows them (``live``: the ``Arena.view`` of the poll)."""

    poll: Poll
    tap_start: dt.datetime
    tap_end: dt.datetime
    phase: BattlePhase
    left: int  # whole seconds left in the phase, rounded up (0 once over)
    live: Mapping[str, Any] | None = None

    @property
    def tap_start_ms(self) -> int:
        return int(self.tap_start.timestamp() * 1000)

    @property
    def tap_end_ms(self) -> int:
        return int(self.tap_end.timestamp() * 1000)

    def _series(self, key: str) -> list[int]:
        values = list(self.live[key]) if self.live else []
        count = len(self.poll.options)
        return (values + [0] * count)[:count]

    @property
    def levels(self) -> list[int]:
        """Taps of each option in the last moments, in the order of the options."""
        return self._series("levels")

    @property
    def totals(self) -> list[int]:
        return self._series("totals")

    @property
    def phones(self) -> int:
        return int(self.live["phones"]) if self.live else 0

    @property
    def peak(self) -> int:
        """The level the gauges are drawn against at first: the highest one now."""
        return max([1, *self.levels])


def battle_of(
    poll: Poll, now: dt.datetime, live: Mapping[str, Any] | None = None
) -> ScreenBattle | None:
    """The battle of an open aura poll, else None."""
    if not poll.is_aura or poll.status is not PollStatus.OPEN or poll.opened_at is None:
        return None
    tap_start = poll.opened_at + dt.timedelta(seconds=aura.CHOOSE_SECONDS)
    tap_end = poll.closes_at or tap_start
    if now < tap_start:
        phase, until = BattlePhase.CHOOSE, tap_start
    elif now < tap_end:
        phase, until = BattlePhase.TAPS, tap_end
    else:
        phase, until = BattlePhase.OVER, now
    left = math.ceil((until - now).total_seconds())
    return ScreenBattle(poll, tap_start, tap_end, phase, left, live)


@dataclass(frozen=True, slots=True)
class AgendaLine:
    position: int  # 1, 2, …
    title: str
    current: bool  # the item under discussion
    past: bool  # before the item under discussion


@dataclass(frozen=True, slots=True)
class AgendaWindow:
    """The agenda items that fit, around the item under discussion."""

    lines: list[AgendaLine]
    before: int  # items left out before the window
    after: int  # items left out after it


def agenda_window(titles: list[str], current: int | None, size: int) -> AgendaWindow:
    """At most ``size`` items: from the one before the current item (context), or from the
    start when no item is under discussion; the window slides back near the end of the list."""
    count = len(titles)
    size = max(size, 1)
    if current is not None and not 1 <= current <= count:
        current = None  # stale position (items removed meanwhile): highlight nothing
    first = 1 if current is None else current - 1
    first = max(1, min(first, count - size + 1))
    last = min(count, first + size - 1)
    lines = [
        AgendaLine(
            position=position,
            title=titles[position - 1],
            current=position == current,
            past=current is not None and position < current,
        )
        for position in range(first, last + 1)
    ]
    return AgendaWindow(lines=lines, before=first - 1, after=count - last)


@dataclass(frozen=True, slots=True)
class ScreenView:
    """Everything the projection screen shows."""

    assembly: Assembly
    # Voters may still join: the session is not closed and entries are not locked (FR-27).
    entries_open: bool
    # The session password, only while it may be shown: password mode and entries open.
    password: str | None
    attendees: int
    quorum: int | None
    open_polls: list[ScreenPoll]  # accepting votes, most recently pushed first
    # Put to the vote and no longer accepting votes (closed, published, cancelled, or timer run
    # out), most recently pushed first. Shown with their state, or the verdict of a result the
    # tribune shows (``result_of``).
    past_polls: list[Poll]
    results: list[ScreenResult]  # shown by the tribune (FR-75), most recently shown first
    timer: TimerState | None  # None while hidden
    agenda: list[str]  # item titles, in order
    agenda_hidden: bool = False  # by the tribune (``show_part``)
    join_hidden: bool = False  # by the tribune, while entries are open (``show_part``)
    # An aura battle under way: the stage to itself, the other blocks wait (SPEC §6.8). Never
    # among ``open_polls`` nor ``past_polls`` until it is closed.
    battle: ScreenBattle | None = None

    @property
    def in_session(self) -> bool:
        return self.assembly.status is AssemblyStatus.IN_SESSION

    @property
    def closed(self) -> bool:
        return self.assembly.status is AssemblyStatus.CLOSED

    @property
    def codes_mode(self) -> bool:
        return self.assembly.access_mode is AccessMode.CODES

    @property
    def join_shown(self) -> bool:
        """How to join is on the screen: entries open, and the tribune does not hide it."""
        return self.entries_open and not self.join_hidden

    @property
    def agenda_shown(self) -> bool:
        return bool(self.agenda) and not self.agenda_hidden

    @property
    def quorum_reached(self) -> bool:
        return self.quorum is not None and self.attendees >= self.quorum

    @property
    def entries(self) -> Entries:
        if self.closed:
            return Entries.ENDED
        if self.assembly.entries_locked:
            return Entries.LOCKED
        if self.assembly.status is AssemblyStatus.PREPARATION:
            return Entries.SOON
        return Entries.OPEN

    @property
    def agenda_current(self) -> int | None:
        return self.assembly.agenda_current

    def agenda_window(self, size: int) -> AgendaWindow:
        return agenda_window(self.agenda, self.agenda_current, size)

    @property
    def stage_result(self) -> ScreenResult | None:
        """The latest result shown, which takes the main place: on the stage, or next to the
        polls open now (FR-75). After the session the closing message keeps the stage, and
        results keep their line in the questions put to the vote."""
        return None if self.closed or not self.results else self.results[0]

    def result_of(self, poll: Poll) -> ScreenResult | None:
        """The result of ``poll`` when the tribune shows it, for its line in the list."""
        return next((item for item in self.results if item.poll.id == poll.id), None)

    @property
    def results_off(self) -> list[Poll]:
        """The closed polls whose result the tribune does not show, most recently pushed first:
        those it may show (FR-75). Never an open or cancelled one."""
        shown = {item.poll.id for item in self.results}
        return [
            poll
            for poll in self.past_polls
            if poll.status in (PollStatus.CLOSED, PollStatus.PUBLISHED)
            and poll.result is not None
            and poll.id not in shown
        ]

    @property
    def past_listed(self) -> list[Poll]:
        """The questions put to the vote listed in one line each: every one but the result in
        the main place, which is not repeated."""
        stage = self.stage_result
        return [poll for poll in self.past_polls if stage is None or poll.id != stage.poll.id]

    def _parts(self) -> list[Part]:
        parts = []
        if self.timer is not None:
            parts.append(Part.TIMER)
        if self.join_shown:
            parts.append(Part.JOIN)
        if self.agenda_shown:
            parts.append(Part.AGENDA)
        if self.open_polls or self.past_polls:
            parts.append(Part.QUESTIONS)
        return parts

    @property
    def hero(self) -> Part:
        """The block given the stage: an open vote first (with the latest result shown next to
        it), then the latest result shown, how to join, the chronometer, the agenda, the
        questions already put to the vote, else a message."""
        if self.closed:
            return Part.MESSAGE
        if self.battle is not None:
            return Part.BATTLE
        if self.open_polls:
            return Part.QUESTIONS
        if self.stage_result is not None:
            return Part.RESULT
        parts = self._parts()
        for part in (Part.JOIN, Part.TIMER, Part.AGENDA, Part.QUESTIONS):
            if part in parts:
                return part
        return Part.MESSAGE

    @property
    def side(self) -> list[Part]:
        """The other blocks, in a side column from top to bottom. Below a result on the stage,
        the questions put to the vote take a strip of the stage, as below open polls."""
        if self.closed:
            return [Part.QUESTIONS] if self.past_polls else []
        hero = self.hero
        if hero is Part.BATTLE:
            return []  # the chronometer, the agenda and the way to join come back after it
        return [
            part
            for part in self._parts()
            if part is not hero and not (hero is Part.RESULT and part is Part.QUESTIONS)
        ]


def _pushed_order(poll: Poll) -> tuple[dt.datetime, int]:
    return (poll.opened_at or _EPOCH, poll.id)


def _shown_order(poll: Poll) -> tuple[dt.datetime, int]:
    return (poll.screen_shown_at or _EPOCH, poll.id)


def _turnouts(db: Session, poll_ids: list[int]) -> dict[int, int]:
    if not poll_ids:
        return {}
    rows = db.execute(
        select(Participation.poll_id, func.count(Participation.credential_id))
        .where(Participation.poll_id.in_(poll_ids))
        .group_by(Participation.poll_id)
    )
    return {poll_id: count for poll_id, count in rows}


def _no_taps(_poll_id: int) -> Mapping[str, Any] | None:
    return None


def screen_view(
    db: Session,
    keyring: Keyring,
    assembly: Assembly,
    *,
    taps: Callable[[int], Mapping[str, Any] | None] = _no_taps,
    now: dt.datetime | None = None,
) -> ScreenView:
    """The screen of ``assembly``, already checked against the secret link. ``taps`` gives the
    gauges and totals of an aura battle under way (``Arena.view``), when the process has them."""
    now = now or utcnow()
    entries_open = assembly.status is not AssemblyStatus.CLOSED and not assembly.entries_locked
    join_shown = entries_open and not assembly.screen_join_hidden
    password = None
    if join_shown and assembly.access_mode is AccessMode.PASSWORD:
        # Decrypted only while it may be shown: a locked session, or a way to join the tribune
        # hides, never sends it to the screen.
        password = keyring.decrypt(assembly.access_password_enc)

    polls = sorted(
        db.scalars(
            select(Poll)
            .options(selectinload(Poll.options), selectinload(Poll.result))
            .where(Poll.assembly_id == assembly.id, Poll.status != PollStatus.DRAFT)
        ).all(),
        key=_pushed_order,
        reverse=True,
    )
    # An aura battle keeps the stage until it is closed, frozen past its end.
    battle = next(
        (found for poll in polls if (found := battle_of(poll, now, taps(poll.id))) is not None),
        None,
    )
    if battle is not None:
        polls = [poll for poll in polls if poll.id != battle.poll.id]
    # A poll whose timer ran out stops accepting votes before the periodic job closes it.
    accepting = [poll for poll in polls if is_accepting_votes(poll, now)]
    turnouts = _turnouts(db, [poll.id for poll in accepting])
    # Only closed polls the tribune shows: never an open or cancelled one, whatever the flag.
    shown = sorted(
        (
            poll
            for poll in polls
            if poll.shown_on_screen and poll.status in (PollStatus.CLOSED, PollStatus.PUBLISHED)
        ),
        key=_shown_order,
        reverse=True,
    )

    shown_timer = assembly.timer_shown and assembly.status is not AssemblyStatus.CLOSED
    return ScreenView(
        assembly=assembly,
        entries_open=entries_open,
        password=password,
        attendees=access.attendees(db, assembly),
        quorum=assemblies.quorum_required(assembly),
        open_polls=[ScreenPoll(poll, turnouts.get(poll.id, 0)) for poll in accepting],
        past_polls=[poll for poll in polls if poll not in accepting],
        results=[ScreenResult(poll, poll.result.data) for poll in shown if poll.result],
        timer=timer.state(assembly, now=now) if shown_timer else None,
        # Every item, even a malformed one: ``agenda_current`` counts positions in this list.
        agenda=[str(item.get("title", "")) for item in assembly.agenda or []],
        agenda_hidden=assembly.screen_agenda_hidden,
        join_hidden=assembly.screen_join_hidden,
        battle=battle,
    )


# The blocks ``show_part`` hides or shows, with the column that keeps the choice.
HIDDEN_COLUMNS = {Part.AGENDA: "screen_agenda_hidden", Part.JOIN: "screen_join_hidden"}


def show_part(
    db: Session,
    owner: User,
    public_id: str,
    part: Part | str,
    shown: bool,
    *,
    now: dt.datetime | None = None,
) -> Assembly:
    """Show or hide the agenda or the way to join on the projection screen, until the session
    ends. The way to join still needs entries open to show. Asking for the current state
    changes nothing and logs nothing; each change is logged (FR-80)."""
    now = now or utcnow()
    try:
        block = Part(part)
    except ValueError:
        raise InvalidInputError("screen_unknown_part", "part") from None
    if block not in HIDDEN_COLUMNS:
        raise InvalidInputError("screen_unknown_part", "part")
    assembly = assemblies.get_assembly(db, owner, public_id)
    require_status(assembly, AssemblyStatus.PREPARATION, AssemblyStatus.IN_SESSION)
    column = HIDDEN_COLUMNS[block]
    if getattr(assembly, column) is not shown:
        return assembly
    setattr(assembly, column, not shown)
    audit.record(
        db,
        f"screen.{block.value}_{'shown' if shown else 'hidden'}",
        user_id=owner.id,
        assembly_id=assembly.id,
        now=now,
    )
    return assembly
