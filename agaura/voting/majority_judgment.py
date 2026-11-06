"""Majority Judgment (Balinski & Laraki): each voter grades every option.

Parameters (all optional)::

    {"grade_count": <int>}   # size of the grade scale, 2 to 10, default 6

Grades are integers from ``0`` (best) to ``grade_count - 1`` (worst, "À rejeter"). Labels belong
to the user interface. With the default scale: 0 Très bien, 1 Bien, 2 Assez bien, 3 Passable,
4 Insuffisant, 5 À rejeter.

Ballot content: ``{"grades": {"<option id>": <grade>, ...}}`` with at least one graded option.
Ungraded options receive the worst grade. The normalized content lists every option.

Majority grade: the best grade ``g`` such that more than half of the valid votes give ``g`` or
better, i.e. the lower median. With grades sorted best first, it sits at index ``n // 2``.

Ranking: by *majority value*, the sequence of majority grades obtained by repeatedly removing
one majority grade from the option's grades. Sequences compare lexicographically; identical
sequences (identical grade profiles) are a tie.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
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
    option_from,
    rank_by,
)

DEFAULT_GRADE_COUNT = 6
MIN_GRADE_COUNT = 2
MAX_GRADE_COUNT = 10


@dataclass(frozen=True, slots=True)
class Params:
    grade_count: int


@dataclass(frozen=True, slots=True)
class OptionTally:
    option: OptionId
    profile: tuple[int, ...]  # number of votes per grade, best grade first
    majority_grade: int | None  # None when there is no valid vote
    share_above: Fraction | None  # share of votes strictly better than the majority grade
    share_below: Fraction | None  # share of votes strictly worse than the majority grade


@dataclass(frozen=True, slots=True)
class Outcome:
    status: Status
    grade_count: int
    options: tuple[OptionTally, ...]
    ranking: Ranking
    winner: OptionId | None
    tied: tuple[OptionId, ...]  # options sharing the top when no single winner


@lru_cache(maxsize=16)
def majority_value_order(n: int) -> tuple[int, ...]:
    """Indices, in a best-first sorted list of ``n`` grades, of the successive majority grades.

    The positions do not depend on the grades themselves, so they are computed once per ``n``.
    """
    remaining = list(range(n))
    order = []
    while remaining:
        order.append(remaining.pop(len(remaining) // 2))
    return tuple(order)


class MajorityJudgment:
    name = "majority_judgment"

    def parse_params(self, options: tuple[OptionId, ...], raw: Mapping[str, Any]) -> Params:
        check_keys(raw, required=set(), optional={"grade_count"}, what="params")
        grade_count = raw.get("grade_count", DEFAULT_GRADE_COUNT)
        if not is_int(grade_count) or not MIN_GRADE_COUNT <= grade_count <= MAX_GRADE_COUNT:
            raise InvalidSpecError("invalid_param", "grade_count")
        return Params(grade_count=grade_count)

    def validate(
        self, params: Params, options: tuple[OptionId, ...], content: Mapping[str, Any]
    ) -> dict[str, Any]:
        grades = content.get("grades")
        if set(content) != {"grades"} or not isinstance(grades, Mapping):
            raise InvalidBallotError("malformed_ballot")
        if not grades:
            raise InvalidBallotError("empty_ballot")
        known = frozenset(options)
        given: dict[OptionId, int] = {}
        for key, grade in grades.items():
            option = option_from(key, known)
            if option is None:
                raise InvalidBallotError("unknown_option")
            if option in given:
                raise InvalidBallotError("duplicate_option")
            if not is_int(grade) or not 0 <= grade < params.grade_count:
                raise InvalidBallotError("invalid_grade")
            given[option] = grade
        worst = params.grade_count - 1
        return {"grades": {str(option): given.get(option, worst) for option in options}}

    def tally(
        self,
        params: Params,
        options: tuple[OptionId, ...],
        contents: Sequence[Mapping[str, Any]],
        counts: Counts,
        attendees: int | None,
    ) -> Outcome:
        n = counts.valid
        sorted_grades = {
            option: sorted(content["grades"][str(option)] for content in contents)
            for option in options
        }

        def tally_option(option: OptionId) -> OptionTally:
            grades = sorted_grades[option]
            profile = tuple(grades.count(grade) for grade in range(params.grade_count))
            if n == 0:
                return OptionTally(option, profile, None, None, None)
            majority = grades[n // 2]
            better = sum(profile[:majority])
            worse = sum(profile[majority + 1 :])
            return OptionTally(option, profile, majority, Fraction(better, n), Fraction(worse, n))

        tallies = tuple(tally_option(option) for option in options)
        if n == 0:
            return Outcome(Status.NO_VALID_VOTES, params.grade_count, tallies, (), None, ())

        order = majority_value_order(n)
        ranking = rank_by(
            options, key=lambda option: tuple(sorted_grades[option][i] for i in order)
        )
        top = ranking[0]
        return Outcome(
            status=Status.DECIDED if len(top) == 1 else Status.TIE,
            grade_count=params.grade_count,
            options=tallies,
            ranking=ranking,
            winner=top[0] if len(top) == 1 else None,
            tied=top if len(top) > 1 else (),
        )
