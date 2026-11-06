from fractions import Fraction

import pytest

from agaura.voting import (
    Ballot,
    InvalidBallotError,
    InvalidSpecError,
    PollSpec,
    Status,
    check_spec,
    tally,
    validate_ballot,
)

A, B, C, D = 1, 2, 3, 4
OPTIONS = (A, B, C, D)


def spec(**params: object) -> PollSpec:
    return PollSpec("approval", OPTIONS, params)


def ballots(*approved: tuple[int, ...], blank: int = 0) -> list[Ballot]:
    return [Ballot({"approved": list(group)}) for group in approved] + [Ballot()] * blank


# A: 3 approvals, B: 2, C: 2, D: 0 (out of 4 valid ballots)
SAMPLE = ballots((A, B), (A, B), (A, C), (C,), blank=1)


class TestParams:
    @pytest.mark.parametrize(
        "params",
        [
            {"seats": 0},
            {"seats": 5},
            {"seats": True},
            {"seats": "2"},
            {"max_choices": 0},
            {"max_choices": "2"},
            {"min_share": 0.5},
        ],
    )
    def test_rejects_invalid_values(self, params: dict[str, object]) -> None:
        with pytest.raises(InvalidSpecError) as error:
            check_spec(spec(**params))
        assert error.value.code == "invalid_param"

    def test_rejects_unknown_parameters(self) -> None:
        with pytest.raises(InvalidSpecError, match="unknown_param"):
            check_spec(spec(winners=2))


class TestValidation:
    def test_normalizes_to_sorted_ids(self) -> None:
        assert validate_ballot(spec(), {"approved": ["3", 1]}) == {"approved": [1, 3]}

    @pytest.mark.parametrize(
        ("content", "code"),
        [
            ({"approved": 1}, "malformed_ballot"),
            ({"approved": [1], "extra": 1}, "malformed_ballot"),
            ({}, "malformed_ballot"),
            ({"approved": [9]}, "unknown_option"),
            ({"approved": [True]}, "unknown_option"),
            ({"approved": []}, "empty_ballot"),
            ({"approved": [1, "1"]}, "duplicate_option"),
        ],
    )
    def test_rejects_invalid_ballots(self, content: dict[str, object], code: str) -> None:
        with pytest.raises(InvalidBallotError) as error:
            validate_ballot(spec(), content)
        assert error.value.code == code

    def test_enforces_max_choices(self) -> None:
        validate_ballot(spec(max_choices=2), {"approved": [1, 2]})
        with pytest.raises(InvalidBallotError, match="too_many_choices"):
            validate_ballot(spec(max_choices=2), {"approved": [1, 2, 3]})


class TestTally:
    def test_counts_and_shares(self) -> None:
        result = tally(spec(), SAMPLE)
        assert (result.counts.cast, result.counts.blank, result.counts.valid) == (5, 1, 4)
        assert [(o.option, o.approvals, o.share) for o in result.outcome.options] == [
            (A, 3, Fraction(3, 4)),
            (B, 2, Fraction(1, 2)),
            (C, 2, Fraction(1, 2)),
            (D, 0, Fraction(0)),
        ]
        assert result.outcome.ranking == ((A,), (B, C), (D,))

    def test_single_seat(self) -> None:
        outcome = tally(spec(), SAMPLE).outcome
        assert outcome.status is Status.DECIDED
        assert outcome.elected == (A,)

    def test_tie_for_the_last_seat_is_reported(self) -> None:
        outcome = tally(spec(seats=2), SAMPLE).outcome
        assert outcome.status is Status.TIE
        assert (outcome.elected, outcome.tied, outcome.seats_in_tie) == ((A,), (B, C), 1)
        assert outcome.unfilled_seats == 0

    def test_tied_group_fitting_the_seats_is_elected(self) -> None:
        outcome = tally(spec(seats=3), SAMPLE).outcome
        assert outcome.status is Status.DECIDED
        assert outcome.elected == (A, B, C)

    def test_options_without_approval_are_never_elected(self) -> None:
        outcome = tally(spec(seats=4), SAMPLE).outcome
        assert outcome.status is Status.SEATS_UNFILLED
        assert (outcome.elected, outcome.unfilled_seats) == ((A, B, C), 1)
        assert outcome.options[3].eligible is False

    def test_minimum_share(self) -> None:
        params = {"seats": 2, "min_share": {"fraction": "1/2", "strict": True}}
        outcome = tally(spec(**params), SAMPLE).outcome
        assert outcome.status is Status.SEATS_UNFILLED
        assert (outcome.elected, outcome.unfilled_seats) == ((A,), 1)
        assert [o.eligible for o in outcome.options] == [True, False, False, False]

    def test_no_valid_votes(self) -> None:
        outcome = tally(spec(seats=2), ballots(blank=2)).outcome
        assert outcome.status is Status.NO_VALID_VOTES
        assert (outcome.elected, outcome.unfilled_seats) == ((), 2)
        assert all(o.share is None for o in outcome.options)
