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
from agaura.voting.majority_judgment import Outcome, majority_value_order


def spec(options: tuple[int, ...] = (1, 2, 3), **params: object) -> PollSpec:
    return PollSpec("majority_judgment", options, params)


def graded(*rows: dict[int, int]) -> list[Ballot]:
    return [Ballot({"grades": row}) for row in rows]


class TestParams:
    @pytest.mark.parametrize("grade_count", [1, 11, True, "6"])
    def test_rejects_invalid_scales(self, grade_count: object) -> None:
        with pytest.raises(InvalidSpecError, match="invalid_param"):
            check_spec(spec(grade_count=grade_count))

    def test_rejects_unknown_parameters(self) -> None:
        with pytest.raises(InvalidSpecError, match="unknown_param"):
            check_spec(spec(labels=["a", "b"]))


class TestValidation:
    def test_fills_ungraded_options_with_the_worst_grade(self) -> None:
        assert validate_ballot(spec(), {"grades": {1: 0, "3": 2}}) == {
            "grades": {"1": 0, "2": 5, "3": 2}
        }

    @pytest.mark.parametrize(
        ("content", "code"),
        [
            ({"grades": [0, 1]}, "malformed_ballot"),
            ({"grades": {1: 0}, "extra": 1}, "malformed_ballot"),
            ({}, "malformed_ballot"),
            ({"grades": {}}, "empty_ballot"),
            ({"grades": {9: 0}}, "unknown_option"),
            ({"grades": {1: 0, "1": 1}}, "duplicate_option"),
            ({"grades": {1: 6}}, "invalid_grade"),
            ({"grades": {1: -1}}, "invalid_grade"),
            ({"grades": {1: True}}, "invalid_grade"),
            ({"grades": {1: "2"}}, "invalid_grade"),
        ],
    )
    def test_rejects_invalid_ballots(self, content: dict[str, object], code: str) -> None:
        with pytest.raises(InvalidBallotError) as error:
            validate_ballot(spec(), content)
        assert error.value.code == code


class TestMajorityValueOrder:
    def test_starts_at_the_lower_median_and_moves_outwards(self) -> None:
        assert majority_value_order(4) == (2, 1, 3, 0)
        assert majority_value_order(5) == (2, 3, 1, 4, 0)
        assert majority_value_order(0) == ()


class TestTennesseeCapital:
    """Published example: en.wikipedia.org/wiki/Majority_judgment#Example_application.

    Four grades (0 Excellent, 1 Good, 2 Fair, 3 Poor) and 100 voters: 42% live in Memphis,
    26% in Nashville, 15% in Chattanooga, 17% in Knoxville. Published result: medians Poor,
    Fair, Fair, Fair (signs +, +, -, -); the tie between the three "Fair" cities is broken in
    favour of Nashville.
    """

    MEMPHIS, NASHVILLE, CHATTANOOGA, KNOXVILLE = 1, 2, 3, 4
    E, G, F, P = 0, 1, 2, 3

    @pytest.fixture
    def outcome(self) -> Outcome:
        m, n, c, k = self.MEMPHIS, self.NASHVILLE, self.CHATTANOOGA, self.KNOXVILLE
        exc, good, fair, poor = self.E, self.G, self.F, self.P
        rows = (
            [{m: exc, n: fair, c: poor, k: poor}] * 42
            + [{m: poor, n: exc, c: fair, k: fair}] * 26
            + [{m: poor, n: fair, c: exc, k: good}] * 15
            + [{m: poor, n: fair, c: good, k: exc}] * 17
        )
        return tally(spec((m, n, c, k), grade_count=4), graded(*rows)).outcome

    def test_majority_grades_and_signs(self, outcome: Outcome) -> None:
        summary = {
            o.option: (o.majority_grade, o.share_above, o.share_below) for o in outcome.options
        }
        assert summary == {
            self.MEMPHIS: (self.P, Fraction(42, 100), Fraction(0)),
            self.NASHVILLE: (self.F, Fraction(26, 100), Fraction(0)),
            self.CHATTANOOGA: (self.F, Fraction(32, 100), Fraction(42, 100)),
            self.KNOXVILLE: (self.F, Fraction(32, 100), Fraction(42, 100)),
        }

    def test_nashville_wins(self, outcome: Outcome) -> None:
        assert outcome.status is Status.DECIDED
        assert outcome.winner == self.NASHVILLE
        assert outcome.ranking == (
            (self.NASHVILLE,),
            (self.KNOXVILLE,),
            (self.CHATTANOOGA,),
            (self.MEMPHIS,),
        )

    def test_profiles(self, outcome: Outcome) -> None:
        profiles = {o.option: o.profile for o in outcome.options}
        assert profiles[self.CHATTANOOGA] == (15, 17, 26, 42)
        assert profiles[self.MEMPHIS] == (42, 0, 0, 58)


class TestTally:
    def test_lower_median_with_an_even_number_of_votes(self) -> None:
        outcome = tally(spec(), graded({1: 0, 2: 1}, {1: 5, 2: 1})).outcome
        assert [o.majority_grade for o in outcome.options] == [5, 1, 5]
        assert outcome.winner == 2

    def test_identical_profiles_are_a_tie(self) -> None:
        outcome = tally(spec(), graded({1: 1, 2: 2, 3: 4}, {1: 2, 2: 1, 3: 4})).outcome
        assert outcome.status is Status.TIE
        assert (outcome.winner, outcome.tied) == (None, (1, 2))
        assert outcome.ranking == ((1, 2), (3,))

    def test_blank_ballots_are_excluded(self) -> None:
        result = tally(spec(), [*graded({1: 0}), Ballot()])
        assert (result.counts.valid, result.counts.blank) == (1, 1)
        assert result.outcome.options[0].profile == (1, 0, 0, 0, 0, 0)

    def test_no_valid_votes(self) -> None:
        outcome = tally(spec(), [Ballot()]).outcome
        assert outcome.status is Status.NO_VALID_VOTES
        assert (outcome.ranking, outcome.winner, outcome.tied) == ((), None, ())
        assert outcome.options[0].majority_grade is None
        assert outcome.options[0].profile == (0,) * 6
