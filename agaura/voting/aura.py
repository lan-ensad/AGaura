"""Aura battle (SPEC §6.8): a game of the room, with no voting value and no ballot.

Voters pick an option on their phone during ``CHOOSE_SECONDS``, then tap a button for the
duration of the battle; the server only counts the taps of each option (``agaura/aura.py``) and
this module ranks the totals once the battle is over. Parameters (optional)::

    {"duration": 5}    # seconds of the tap phase: 5 (default), 10, 15 or 20

2 to ``MAX_OPTIONS`` options. Every ballot is refused (``validate``): a battle has none, so the
engine's ``tally`` sees no ballot and ranks nobody. ``rank`` puts the most taps first; equal
totals are tied, reported and never broken (D6); a battle nobody tapped in has no winner.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from agaura.voting.base import (
    Counts,
    InvalidBallotError,
    InvalidSpecError,
    OptionId,
    Ranking,
    Status,
    check_keys,
    is_int,
    rank_by,
    share,
)

CHOOSE_SECONDS = 10  # to pick an option, from the push of the battle
DURATIONS = (5, 10, 15, 20)  # seconds of the tap phase the tribune may choose
DEFAULT_DURATION = 5
GRACE_SECONDS = 1  # taps sent before the end still count this long after it (in flight)
MAX_OPTIONS = 6  # one gauge each, side by side on the projection screen


@dataclass(frozen=True, slots=True)
class Params:
    duration: int


@dataclass(frozen=True, slots=True)
class Taps:
    """What a battle leaves once over: the taps counted for each option, the number of phones
    that tapped, and whether taps may have been lost (a restart of the server)."""

    totals: Mapping[OptionId, int]
    phones: int
    interrupted: bool = False


@dataclass(frozen=True, slots=True)
class OptionTally:
    option: OptionId
    taps: int
    share: Fraction | None  # of every tap of the battle, None when nobody tapped


@dataclass(frozen=True, slots=True)
class Outcome:
    status: Status  # DECIDED, TIE (at the top) or NO_VALID_VOTES (nobody tapped)
    taps: int  # every tap counted
    phones: int
    interrupted: bool
    options: tuple[OptionTally, ...]
    ranking: Ranking
    winners: tuple[OptionId, ...]  # the top group once someone tapped: one, or a tie


def parse(options: Sequence[OptionId], raw: Mapping[str, Any]) -> Params:
    """The parameters of a battle, or ``InvalidSpecError``."""
    check_keys(raw, required=set(), optional={"duration"}, what="params")
    if len(options) > MAX_OPTIONS:
        raise InvalidSpecError("option_count")
    duration = raw.get("duration", DEFAULT_DURATION)
    if not is_int(duration) or duration not in DURATIONS:
        raise InvalidSpecError("invalid_param", "duration")
    return Params(duration=duration)


def rank(options: Sequence[OptionId], taps: Taps) -> Outcome:
    """Rank the options of a battle by their taps, most first; ties reported, never broken."""
    known = frozenset(options)
    totals: dict[OptionId, int] = {}
    for option, count in taps.totals.items():
        if option not in known:
            raise InvalidSpecError("unknown_option", str(option))
        if not is_int(count) or count < 0:
            raise InvalidSpecError("invalid_taps", str(option))
        totals[option] = count
    if not is_int(taps.phones) or taps.phones < 0:
        raise InvalidSpecError("invalid_taps", "phones")
    total = sum(totals.values())
    ranking = rank_by(options, key=lambda option: -totals.get(option, 0))
    winners = ranking[0] if total else ()
    if not total:
        status = Status.NO_VALID_VOTES
    elif len(winners) > 1:
        status = Status.TIE
    else:
        status = Status.DECIDED
    return Outcome(
        status=status,
        taps=total,
        phones=taps.phones,
        interrupted=taps.interrupted,
        options=tuple(
            OptionTally(option, totals.get(option, 0), share(totals.get(option, 0), total))
            for option in options
        ),
        ranking=ranking,
        winners=winners,
    )


class Aura:
    """The engine's view of a battle: parameters checked, every ballot refused."""

    name = "aura"

    def parse_params(self, options: tuple[OptionId, ...], raw: Mapping[str, Any]) -> Params:
        return parse(options, raw)

    def validate(
        self, params: Params, options: tuple[OptionId, ...], content: Mapping[str, Any]
    ) -> dict[str, Any]:
        raise InvalidBallotError("no_ballot")

    def tally(
        self,
        params: Params,
        options: tuple[OptionId, ...],
        contents: Sequence[Mapping[str, Any]],
        counts: Counts,
        attendees: int | None,
    ) -> Outcome:
        # No ballot is ever valid, so nothing reaches here but blank ones: nobody tapped.
        return rank(options, Taps(totals={}, phones=0))
