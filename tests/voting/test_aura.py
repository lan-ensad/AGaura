from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agaura.voting import (
    Ballot,
    InvalidBallotError,
    InvalidSpecError,
    PollSpec,
    Status,
    check_spec,
    tally,
    to_jsonable,
    validate_ballot,
)
from agaura.voting.aura import DEFAULT_DURATION, DURATIONS, Taps, parse, rank

A, B, C = 1, 2, 3
OPTIONS = (A, B, C)


class TestParams:
    def test_default_duration(self) -> None:
        assert parse(OPTIONS, {}).duration == DEFAULT_DURATION == 5

    @pytest.mark.parametrize("duration", DURATIONS)
    def test_durations(self, duration: int) -> None:
        assert parse(OPTIONS, {"duration": duration}).duration == duration
        check_spec(PollSpec("aura", OPTIONS, {"duration": duration}))

    @pytest.mark.parametrize("duration", [0, 4, 7, 25, 30, True, "5", 5.0, None])
    def test_other_durations_refused(self, duration: object) -> None:
        with pytest.raises(InvalidSpecError, match="invalid_param: duration"):
            check_spec(PollSpec("aura", OPTIONS, {"duration": duration}))

    def test_unknown_param(self) -> None:
        with pytest.raises(InvalidSpecError, match="unknown_param"):
            check_spec(PollSpec("aura", OPTIONS, {"seats": 1}))

    def test_two_to_six_options(self) -> None:
        check_spec(PollSpec("aura", (1, 2)))
        check_spec(PollSpec("aura", tuple(range(1, 7))))
        for options in [(1,), tuple(range(1, 8))]:
            with pytest.raises(InvalidSpecError, match="option_count"):
                check_spec(PollSpec("aura", options))


def test_no_ballot_is_valid() -> None:
    with pytest.raises(InvalidBallotError, match="no_ballot"):
        validate_ballot(PollSpec("aura", OPTIONS), {"choice": A})


def test_the_engine_ranks_nobody() -> None:
    """A battle has no ballot: tallying blank ones (never stored) ranks nobody."""
    result = tally(PollSpec("aura", OPTIONS), [Ballot()])
    assert result.outcome.status is Status.NO_VALID_VOTES
    assert result.outcome.winners == ()


class TestRank:
    def test_most_taps_first(self) -> None:
        outcome = rank(OPTIONS, Taps({A: 40, B: 120, C: 40}, phones=12))
        assert outcome.status is Status.DECIDED
        assert outcome.ranking == ((B,), (A, C))
        assert outcome.winners == (B,)
        assert (outcome.taps, outcome.phones, outcome.interrupted) == (200, 12, False)
        assert [(item.option, item.taps, item.share) for item in outcome.options] == [
            (A, 40, Fraction(1, 5)),
            (B, 120, Fraction(3, 5)),
            (C, 40, Fraction(1, 5)),
        ]

    def test_tie_at_the_top_reported(self) -> None:
        outcome = rank(OPTIONS, Taps({A: 50, B: 50, C: 3}, phones=4))
        assert outcome.status is Status.TIE
        assert outcome.winners == (A, B)

    def test_nobody_tapped(self) -> None:
        outcome = rank(OPTIONS, Taps({}, phones=0, interrupted=True))
        assert outcome.status is Status.NO_VALID_VOTES
        assert outcome.winners == ()
        assert outcome.ranking == ((A, B, C),)
        assert all(item.share is None for item in outcome.options)
        assert outcome.interrupted

    @pytest.mark.parametrize(
        ("totals", "phones", "code"),
        [
            ({4: 1}, 1, "unknown_option"),
            ({A: -1}, 1, "invalid_taps"),
            ({A: True}, 1, "invalid_taps"),
            ({A: 1}, -1, "invalid_taps"),
        ],
    )
    def test_refused(self, totals: dict[int, int], phones: int, code: str) -> None:
        with pytest.raises(InvalidSpecError, match=code):
            rank(OPTIONS, Taps(totals, phones=phones))

    def test_jsonable(self) -> None:
        stored = to_jsonable(rank(OPTIONS, Taps({A: 1, B: 3}, phones=2)))
        assert stored["status"] == "decided"
        assert stored["winners"] == [B]
        assert stored["options"][1] == {"option": B, "taps": 3, "share": "3/4"}


@given(st.lists(st.integers(min_value=0, max_value=10_000), min_size=2, max_size=6))
def test_ranking_follows_the_totals(counts: list[int]) -> None:
    options = tuple(range(1, len(counts) + 1))
    outcome = rank(options, Taps(dict(zip(options, counts, strict=True)), phones=1))
    flat = [option for group in outcome.ranking for option in group]
    assert sorted(flat) == list(options)
    taps = [counts[option - 1] for option in flat]
    assert taps == sorted(taps, reverse=True)
    for group in outcome.ranking:
        assert len({counts[option - 1] for option in group}) == 1
    if sum(counts):
        top = max(counts)
        assert set(outcome.winners) == {o for o in options if counts[o - 1] == top}
    else:
        assert outcome.winners == ()
