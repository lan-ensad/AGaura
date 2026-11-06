"""Single choice: each voter picks exactly one option.

Parameters (all optional)::

    {
      "base": "valid" | "cast" | "attendees",       # default "valid"
      "threshold": "plurality" | "majority" | "two_thirds" | "three_quarters"
                   | {"fraction": "3/5", "strict": false},
      "adopt_option": <option id>                   # e.g. "For" in the Resolution template
    }

Default threshold: ``"majority"`` (strictly more than half) with 2 options, ``"plurality"``
(most votes) with 3 or more.

Ballot content: ``{"choice": <option id>}``.

The winner is the option with the most votes, provided it is alone at the top and meets the
threshold. When ``adopt_option`` is set, ``adopted`` tells whether that option meets the
threshold (``None`` when it is tied at the top under plurality).
"""

from __future__ import annotations

import enum
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from agaura.voting.base import (
    Counts,
    InvalidBallotError,
    InvalidSpecError,
    OptionId,
    Quota,
    Ranking,
    Status,
    check_keys,
    option_from,
    parse_quota,
    rank_by,
    share,
)

PRESETS: dict[str, Quota | None] = {
    "plurality": None,
    "majority": Quota(Fraction(1, 2), strict=True),
    "two_thirds": Quota(Fraction(2, 3), strict=False),
    "three_quarters": Quota(Fraction(3, 4), strict=False),
}


class Base(enum.StrEnum):
    """Denominator of the shares."""

    VALID = "valid"  # valid votes (blank ballots excluded)
    CAST = "cast"  # all ballots, blank included
    ATTENDEES = "attendees"  # voter credentials in use


@dataclass(frozen=True, slots=True)
class Params:
    base: Base
    quota: Quota | None  # None means plurality
    adopt_option: OptionId | None


@dataclass(frozen=True, slots=True)
class OptionTally:
    option: OptionId
    votes: int
    share: Fraction | None  # votes / base, None when the base is 0
    meets_threshold: bool


@dataclass(frozen=True, slots=True)
class Outcome:
    status: Status
    base: Base
    base_count: int
    options: tuple[OptionTally, ...]
    ranking: Ranking
    winner: OptionId | None
    tied: tuple[OptionId, ...]  # options sharing the top when no single winner
    adopted: bool | None  # only when adopt_option is set


class SingleChoice:
    name = "single_choice"

    def parse_params(self, options: tuple[OptionId, ...], raw: Mapping[str, Any]) -> Params:
        check_keys(
            raw, required=set(), optional={"base", "threshold", "adopt_option"}, what="params"
        )
        try:
            base = Base(raw.get("base", Base.VALID))
        except ValueError:
            raise InvalidSpecError("invalid_param", "base") from None

        threshold = raw.get("threshold", "majority" if len(options) == 2 else "plurality")
        if isinstance(threshold, str):
            if threshold not in PRESETS:
                raise InvalidSpecError("invalid_param", "threshold")
            quota = PRESETS[threshold]
        else:
            quota = parse_quota(threshold, "threshold")

        adopt_option = None
        if "adopt_option" in raw:
            adopt_option = option_from(raw["adopt_option"], frozenset(options))
            if adopt_option is None:
                raise InvalidSpecError("invalid_param", "adopt_option")
        return Params(base=base, quota=quota, adopt_option=adopt_option)

    def validate(
        self, params: Params, options: tuple[OptionId, ...], content: Mapping[str, Any]
    ) -> dict[str, Any]:
        if set(content) != {"choice"}:
            raise InvalidBallotError("malformed_ballot")
        choice = option_from(content["choice"], frozenset(options))
        if choice is None:
            raise InvalidBallotError("unknown_option")
        return {"choice": choice}

    def tally(
        self,
        params: Params,
        options: tuple[OptionId, ...],
        contents: Sequence[Mapping[str, Any]],
        counts: Counts,
        attendees: int | None,
    ) -> Outcome:
        votes = Counter(content["choice"] for content in contents)

        if params.base is Base.VALID:
            base_count = counts.valid
        elif params.base is Base.CAST:
            base_count = counts.cast
        else:
            if attendees is None or attendees < 0:
                raise InvalidSpecError("attendees_required")
            base_count = attendees

        ranking = rank_by(options, key=lambda option: -votes[option])
        top_votes = max(votes[option] for option in options)
        top = ranking[0] if top_votes > 0 else ()

        def meets(option: OptionId) -> bool:
            if params.quota is None:
                return top == (option,)
            return params.quota.is_met(votes[option], base_count)

        winner = top[0] if len(top) == 1 and meets(top[0]) else None

        if counts.valid == 0:
            status = Status.NO_VALID_VOTES
        elif winner is not None:
            status = Status.DECIDED
        elif params.quota is not None and not any(meets(option) for option in top):
            status = Status.THRESHOLD_NOT_MET
        else:
            status = Status.TIE

        adopted: bool | None = None
        if params.adopt_option is not None:
            undecided = params.quota is None and len(top) > 1 and params.adopt_option in top
            adopted = None if undecided else meets(params.adopt_option)

        return Outcome(
            status=status,
            base=params.base,
            base_count=base_count,
            options=tuple(
                OptionTally(
                    option=option,
                    votes=votes[option],
                    share=share(votes[option], base_count),
                    meets_threshold=meets(option),
                )
                for option in options
            ),
            ranking=ranking,
            winner=winner,
            tied=top if len(top) > 1 else (),
            adopted=adopted,
        )
