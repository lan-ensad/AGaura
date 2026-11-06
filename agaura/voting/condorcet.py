"""Condorcet method, completed by Schulze (*winning votes*): each voter ranks the options.

No parameter: ``{}``.

Ballot content: ``{"ranks": {"<option id>": <rank>, ...}}`` with at least one ranked option.
Ranks are integers from ``1`` (first choice) to the number of options; several options may
share a rank (the voter has no preference between them). Options left unranked come after every
ranked option, tied together. The normalized content lists every option with its dense rank
(``1, 2, 3…`` without gaps), the unranked options at the last one, so that two ballots with the
same preferences have the same content.

Duels: ``pairwise[i][j]`` counts the valid ballots that rank ``options[i]`` strictly above
``options[j]``. An option that wins every duel (``pairwise[i][j] > pairwise[j][i]`` against each
other option) is the *Condorcet winner*; there may be none, when preferences form a cycle.

Schulze: a duel won is a link whose strength is the number of ballots of the winning side (a
duel tied or lost gives no link). The strength of a path is that of its weakest link, and
``strongest_paths[i][j]`` is the strongest path from ``options[i]`` to ``options[j]``
(Floyd-Warshall). ``options[i]`` beats ``options[j]`` when ``strongest_paths[i][j] >
strongest_paths[j][i]``; this relation is transitive, so the Condorcet winner, when there is
one, always wins.

Ranking: the options that no option beats come first, tied together; then the options that only
those beat, and so on. Within a group, every pair has paths of equal strength both ways: a tie.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agaura.voting.base import (
    Counts,
    InvalidBallotError,
    OptionId,
    Ranking,
    Status,
    check_keys,
    is_int,
    option_from,
)

Matrix = tuple[tuple[int, ...], ...]


@dataclass(frozen=True, slots=True)
class Params:
    pass


@dataclass(frozen=True, slots=True)
class OptionTally:
    option: OptionId
    wins: int  # duels won: options it is preferred to by more ballots than the reverse
    losses: int  # duels lost; the other duels are tied


@dataclass(frozen=True, slots=True)
class Outcome:
    status: Status
    options: tuple[OptionTally, ...]
    pairwise: Matrix  # in the order of ``options``; zero on the diagonal
    strongest_paths: Matrix  # same order
    ranking: Ranking
    winner: OptionId | None
    tied: tuple[OptionId, ...]  # options sharing the top when no single winner
    condorcet_winner: OptionId | None  # the option that wins every duel, if any


def _pairwise(options: tuple[OptionId, ...], contents: Sequence[Mapping[str, Any]]) -> Matrix:
    # Identical ballots are counted once and weighed by their number.
    profiles = Counter(
        tuple(content["ranks"][str(option)] for option in options) for content in contents
    )
    size = len(options)
    counts = [[0] * size for _ in range(size)]
    for ranks, number in profiles.items():
        for i in range(size):
            for j in range(size):
                if ranks[i] < ranks[j]:
                    counts[i][j] += number
    return tuple(tuple(row) for row in counts)


def _strongest_paths(pairwise: Matrix) -> Matrix:
    size = len(pairwise)
    paths = [
        [pairwise[i][j] if pairwise[i][j] > pairwise[j][i] else 0 for j in range(size)]
        for i in range(size)
    ]
    for k in range(size):
        for i in range(size):
            if i == k:
                continue
            for j in range(size):
                if j not in (i, k):
                    paths[i][j] = max(paths[i][j], min(paths[i][k], paths[k][j]))
    return tuple(tuple(row) for row in paths)


def _layers(options: tuple[OptionId, ...], paths: Matrix) -> Ranking:
    """Groups of options, best first: each group holds the remaining options none beats.

    The relation "beats" is a strict partial order (Schulze, 2011, §4.1), so every round finds
    at least one option and the loop ends.
    """
    remaining = list(range(len(options)))
    ranking: list[tuple[OptionId, ...]] = []
    while remaining:
        top = [i for i in remaining if not any(paths[j][i] > paths[i][j] for j in remaining)]
        ranking.append(tuple(options[i] for i in top))
        remaining = [i for i in remaining if i not in top]
    return tuple(ranking)


class Condorcet:
    name = "condorcet"

    def parse_params(self, options: tuple[OptionId, ...], raw: Mapping[str, Any]) -> Params:
        check_keys(raw, required=set(), optional=set(), what="params")
        return Params()

    def validate(
        self, params: Params, options: tuple[OptionId, ...], content: Mapping[str, Any]
    ) -> dict[str, Any]:
        ranks = content.get("ranks")
        if set(content) != {"ranks"} or not isinstance(ranks, Mapping):
            raise InvalidBallotError("malformed_ballot")
        if not ranks:
            raise InvalidBallotError("empty_ballot")
        known = frozenset(options)
        given: dict[OptionId, int] = {}
        for key, rank in ranks.items():
            option = option_from(key, known)
            if option is None:
                raise InvalidBallotError("unknown_option")
            if option in given:
                raise InvalidBallotError("duplicate_option")
            if not is_int(rank) or not 1 <= rank <= len(options):
                raise InvalidBallotError("invalid_rank")
            given[option] = rank
        dense = {rank: index for index, rank in enumerate(sorted(set(given.values())), start=1)}
        last = len(dense) + 1
        return {
            "ranks": {
                str(option): dense[given[option]] if option in given else last for option in options
            }
        }

    def tally(
        self,
        params: Params,
        options: tuple[OptionId, ...],
        contents: Sequence[Mapping[str, Any]],
        counts: Counts,
        attendees: int | None,
    ) -> Outcome:
        size = len(options)
        pairwise = _pairwise(options, contents)
        paths = _strongest_paths(pairwise)
        tallies = tuple(
            OptionTally(
                option=option,
                wins=sum(pairwise[i][j] > pairwise[j][i] for j in range(size)),
                losses=sum(pairwise[i][j] < pairwise[j][i] for j in range(size)),
            )
            for i, option in enumerate(options)
        )
        if counts.valid == 0:
            return Outcome(Status.NO_VALID_VOTES, tallies, pairwise, paths, (), None, (), None)

        condorcet_winner = next((row.option for row in tallies if row.wins == size - 1), None)
        ranking = _layers(options, paths)
        top = ranking[0]
        return Outcome(
            status=Status.DECIDED if len(top) == 1 else Status.TIE,
            options=tallies,
            pairwise=pairwise,
            strongest_paths=paths,
            ranking=ranking,
            winner=top[0] if len(top) == 1 else None,
            tied=top if len(top) > 1 else (),
            condorcet_winner=condorcet_winner,
        )
