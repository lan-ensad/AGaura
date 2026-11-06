"""Approval: each voter approves one or more options; used to elect N people.

Parameters (all optional)::

    {
      "seats": <int>,                                  # default 1
      "max_choices": <int> | null,                     # default null (no limit)
      "min_share": {"fraction": "1/2", "strict": false} # share of valid votes to be elected
    }

Ballot content: ``{"approved": [<option id>, ...]}`` with at least one option. Approving no
option is not a valid ballot: the interface submits a blank ballot instead.

Seats go to the most approved options, provided they have at least one approval and meet
``min_share``. When a group of tied options does not fit in the remaining seats, the tie is
reported (``tied`` and ``seats_in_tie``) and the remaining seats stay unallocated.
"""

from __future__ import annotations

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
    is_int,
    option_from,
    parse_quota,
    rank_by,
    share,
)


@dataclass(frozen=True, slots=True)
class Params:
    seats: int
    max_choices: int | None
    min_share: Quota | None


@dataclass(frozen=True, slots=True)
class OptionTally:
    option: OptionId
    approvals: int
    share: Fraction | None  # approvals / valid votes, None when there is no valid vote
    eligible: bool  # at least one approval and min_share met


@dataclass(frozen=True, slots=True)
class Outcome:
    status: Status
    seats: int
    options: tuple[OptionTally, ...]
    ranking: Ranking
    elected: tuple[OptionId, ...]
    tied: tuple[OptionId, ...]  # options competing for the last seats
    seats_in_tie: int  # seats left for the tied options
    unfilled_seats: int  # seats left for lack of eligible options


class Approval:
    name = "approval"

    def parse_params(self, options: tuple[OptionId, ...], raw: Mapping[str, Any]) -> Params:
        check_keys(
            raw, required=set(), optional={"seats", "max_choices", "min_share"}, what="params"
        )
        seats = raw.get("seats", 1)
        if not is_int(seats) or not 1 <= seats <= len(options):
            raise InvalidSpecError("invalid_param", "seats")
        max_choices = raw.get("max_choices")
        if max_choices is not None and (not is_int(max_choices) or max_choices < 1):
            raise InvalidSpecError("invalid_param", "max_choices")
        min_share = raw.get("min_share")
        return Params(
            seats=seats,
            max_choices=max_choices,
            min_share=None if min_share is None else parse_quota(min_share, "min_share"),
        )

    def validate(
        self, params: Params, options: tuple[OptionId, ...], content: Mapping[str, Any]
    ) -> dict[str, Any]:
        approved = content.get("approved")
        if set(content) != {"approved"} or not isinstance(approved, list | tuple):
            raise InvalidBallotError("malformed_ballot")
        known = frozenset(options)
        ids: list[OptionId] = []
        for value in approved:
            option = option_from(value, known)
            if option is None:
                raise InvalidBallotError("unknown_option")
            ids.append(option)
        if not ids:
            raise InvalidBallotError("empty_ballot")
        if len(set(ids)) != len(ids):
            raise InvalidBallotError("duplicate_option")
        if params.max_choices is not None and len(ids) > params.max_choices:
            raise InvalidBallotError("too_many_choices")
        return {"approved": sorted(ids)}

    def tally(
        self,
        params: Params,
        options: tuple[OptionId, ...],
        contents: Sequence[Mapping[str, Any]],
        counts: Counts,
        attendees: int | None,
    ) -> Outcome:
        approvals = Counter(option for content in contents for option in content["approved"])

        def eligible(option: OptionId) -> bool:
            if approvals[option] == 0:
                return False
            return params.min_share is None or params.min_share.is_met(
                approvals[option], counts.valid
            )

        ranking = rank_by(options, key=lambda option: -approvals[option])

        elected: list[OptionId] = []
        tied: tuple[OptionId, ...] = ()
        remaining = params.seats
        for group in ranking:
            candidates = tuple(option for option in group if eligible(option))
            if remaining == 0 or not candidates:
                break
            if len(candidates) <= remaining:
                elected.extend(candidates)
                remaining -= len(candidates)
            else:
                tied = candidates
                break

        if counts.valid == 0:
            status = Status.NO_VALID_VOTES
        elif tied:
            status = Status.TIE
        elif remaining > 0:
            status = Status.SEATS_UNFILLED
        else:
            status = Status.DECIDED

        return Outcome(
            status=status,
            seats=params.seats,
            options=tuple(
                OptionTally(
                    option=option,
                    approvals=approvals[option],
                    share=share(approvals[option], counts.valid),
                    eligible=eligible(option),
                )
                for option in options
            ),
            ranking=ranking,
            elected=tuple(elected),
            tied=tied,
            seats_in_tie=remaining if tied else 0,
            unfilled_seats=0 if tied else remaining,
        )
