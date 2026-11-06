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

FOR, AGAINST = 1, 2


def spec(options: tuple[int, ...] = (FOR, AGAINST), **params: object) -> PollSpec:
    return PollSpec("single_choice", options, params)


def resolution(**params: object) -> PollSpec:
    return spec(adopt_option=FOR, **params)


def ballots(*choices: int, blank: int = 0) -> list[Ballot]:
    return [Ballot({"choice": choice}) for choice in choices] + [Ballot()] * blank


class TestParams:
    @pytest.mark.parametrize(
        "params",
        [
            {"base": "everyone"},
            {"threshold": "unanimity"},
            {"threshold": 0.5},
            {"adopt_option": 9},
        ],
    )
    def test_rejects_invalid_values(self, params: dict[str, object]) -> None:
        with pytest.raises(InvalidSpecError) as error:
            check_spec(spec(**params))
        assert error.value.code == "invalid_param"

    def test_rejects_unknown_parameters(self) -> None:
        with pytest.raises(InvalidSpecError, match="unknown_param"):
            check_spec(spec(quorum=10))

    def test_default_threshold_is_strict_majority_with_two_options(self) -> None:
        outcome = tally(resolution(), ballots(FOR, AGAINST)).outcome
        assert outcome.status is Status.THRESHOLD_NOT_MET
        assert outcome.adopted is False

    def test_default_threshold_is_plurality_with_three_options(self) -> None:
        outcome = tally(spec((1, 2, 3)), ballots(1, 1, 2, 3)).outcome
        assert (outcome.status, outcome.winner) == (Status.DECIDED, 1)


class TestValidation:
    def test_normalizes_the_choice(self) -> None:
        assert validate_ballot(spec(), {"choice": "2"}) == {"choice": 2}

    @pytest.mark.parametrize("content", [{}, {"choice": 1, "extra": 1}, {"choices": 1}])
    def test_rejects_malformed_ballots(self, content: dict[str, object]) -> None:
        with pytest.raises(InvalidBallotError, match="malformed_ballot"):
            validate_ballot(spec(), content)

    @pytest.mark.parametrize("choice", [3, True, None, "x"])
    def test_rejects_unknown_options(self, choice: object) -> None:
        with pytest.raises(InvalidBallotError, match="unknown_option"):
            validate_ballot(spec(), {"choice": choice})


class TestResolution:
    def test_adopted_by_strict_majority_of_valid_votes(self) -> None:
        result = tally(resolution(), ballots(FOR, FOR, FOR, AGAINST, blank=2))
        outcome = result.outcome
        assert (result.counts.cast, result.counts.blank, result.counts.valid) == (6, 2, 4)
        assert outcome.status is Status.DECIDED
        assert (outcome.winner, outcome.adopted) == (FOR, True)
        assert outcome.base_count == 4
        assert [(o.option, o.votes, o.share) for o in outcome.options] == [
            (FOR, 3, Fraction(3, 4)),
            (AGAINST, 1, Fraction(1, 4)),
        ]

    def test_equal_split_is_rejected_and_the_tie_reported(self) -> None:
        outcome = tally(resolution(), ballots(FOR, FOR, AGAINST, AGAINST)).outcome
        assert outcome.status is Status.THRESHOLD_NOT_MET
        assert outcome.adopted is False
        assert outcome.tied == (FOR, AGAINST)

    def test_base_cast_counts_blank_ballots(self) -> None:
        outcome = tally(resolution(base="cast"), ballots(FOR, FOR, FOR, AGAINST, blank=3)).outcome
        assert outcome.base_count == 7
        assert outcome.adopted is False

    def test_base_attendees_uses_the_given_count(self) -> None:
        outcome = tally(resolution(base="attendees"), ballots(*[FOR] * 6, AGAINST), attendees=10)
        assert outcome.outcome.base_count == 10
        assert outcome.outcome.adopted is True

    @pytest.mark.parametrize("attendees", [None, -1])
    def test_base_attendees_requires_a_count(self, attendees: int | None) -> None:
        with pytest.raises(InvalidSpecError, match="attendees_required"):
            tally(resolution(base="attendees"), ballots(FOR), attendees=attendees)

    def test_two_thirds_includes_the_boundary(self) -> None:
        outcome = tally(resolution(threshold="two_thirds"), ballots(FOR, FOR, AGAINST)).outcome
        assert outcome.adopted is True

    def test_three_quarters_not_reached(self) -> None:
        outcome = tally(resolution(threshold="three_quarters"), ballots(FOR, FOR, AGAINST)).outcome
        assert outcome.adopted is False
        assert outcome.status is Status.THRESHOLD_NOT_MET

    def test_custom_strict_threshold(self) -> None:
        params = {"threshold": {"fraction": "2/3", "strict": True}}
        outcome = tally(resolution(**params), ballots(FOR, FOR, AGAINST)).outcome
        assert outcome.adopted is False

    def test_non_strict_half_with_equal_split_is_a_tie_but_adopted(self) -> None:
        params = {"threshold": {"fraction": "1/2"}}
        outcome = tally(resolution(**params), ballots(FOR, AGAINST)).outcome
        assert outcome.status is Status.TIE
        assert outcome.winner is None
        assert outcome.adopted is True


class TestPlurality:
    def test_tie_at_the_top_is_reported(self) -> None:
        outcome = tally(spec((1, 2, 3)), ballots(1, 2, 3, 1, 2)).outcome
        assert outcome.status is Status.TIE
        assert (outcome.winner, outcome.tied) == (None, (1, 2))
        assert outcome.ranking == ((1, 2), (3,))

    def test_adopt_option_tied_at_the_top_is_undecided(self) -> None:
        outcome = tally(spec((1, 2, 3), adopt_option=1), ballots(1, 2)).outcome
        assert outcome.adopted is None

    def test_adopt_option_losing_is_not_adopted(self) -> None:
        outcome = tally(spec((1, 2, 3), adopt_option=1), ballots(2, 2, 1)).outcome
        assert (outcome.winner, outcome.adopted) == (2, False)

    def test_adopt_option_winning_is_adopted(self) -> None:
        outcome = tally(spec((1, 2, 3), threshold="plurality", adopt_option=1), ballots(1)).outcome
        assert outcome.adopted is True

    def test_ranking_lists_every_option(self) -> None:
        outcome = tally(spec((1, 2, 3)), ballots(2, 2, 2, 1)).outcome
        assert outcome.ranking == ((2,), (1,), (3,))
        assert [o.meets_threshold for o in outcome.options] == [False, True, False]


class TestNoValidVotes:
    def test_only_blank_ballots(self) -> None:
        result = tally(resolution(), ballots(blank=3))
        outcome = result.outcome
        assert result.counts.valid == 0
        assert outcome.status is Status.NO_VALID_VOTES
        assert (outcome.winner, outcome.adopted, outcome.tied) == (None, False, ())
        assert all(o.share is None for o in outcome.options)

    def test_no_ballot_at_all(self) -> None:
        result = tally(spec((1, 2, 3)), [])
        assert result.counts.cast == 0
        assert result.outcome.status is Status.NO_VALID_VOTES
        assert result.outcome.ranking == ((1, 2, 3),)
