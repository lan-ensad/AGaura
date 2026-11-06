import pytest

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
from agaura.voting.condorcet import Outcome

A, B, C, D, E = 1, 2, 3, 4, 5


def spec(options: tuple[int, ...] = (A, B, C), **params: object) -> PollSpec:
    return PollSpec("condorcet", options, params)


def ranked(spec_: PollSpec, *orders: tuple[int, str]) -> list[Ballot]:
    """Ballots from ``(count, "ACB")`` pairs: letters best first, one rank each."""
    letters = {"A": A, "B": B, "C": C, "D": D, "E": E}
    ballots = []
    for count, order in orders:
        ranks = {letters[letter]: rank for rank, letter in enumerate(order, start=1)}
        ballots += [Ballot(validate_ballot(spec_, {"ranks": ranks}))] * count
    return ballots


def outcome_of(spec_: PollSpec, ballots: list[Ballot]) -> Outcome:
    outcome = tally(spec_, ballots).outcome
    assert isinstance(outcome, Outcome)
    return outcome


class TestParams:
    def test_takes_no_parameter(self) -> None:
        check_spec(spec())
        with pytest.raises(InvalidSpecError, match="unknown_param"):
            check_spec(spec(seats=1))


class TestValidation:
    def test_normalizes_to_dense_ranks_with_unranked_options_last(self) -> None:
        options = (A, B, C, D)
        assert validate_ballot(spec(options), {"ranks": {"3": 4, 1: 2}}) == {
            "ranks": {"1": 1, "2": 3, "3": 2, "4": 3}
        }

    def test_keeps_ties(self) -> None:
        assert validate_ballot(spec(), {"ranks": {"1": 2, "2": 2, "3": 1}}) == {
            "ranks": {"1": 2, "2": 2, "3": 1}
        }

    def test_one_rank_for_every_option_expresses_no_preference(self) -> None:
        assert validate_ballot(spec(), {"ranks": {"1": 3, "2": 3, "3": 3}}) == {
            "ranks": {"1": 1, "2": 1, "3": 1}
        }

    def test_the_same_preferences_give_the_same_content(self) -> None:
        typed = [{"1": 1, "2": 2}, {"1": 1, "2": 2, "3": 3}, {"1": 2, "2": 3}]
        assert len({str(validate_ballot(spec(), {"ranks": ranks})) for ranks in typed}) == 1

    @pytest.mark.parametrize(
        ("content", "code"),
        [
            ({"ranks": [1, 2]}, "malformed_ballot"),
            ({"ranks": {1: 1}, "extra": 1}, "malformed_ballot"),
            ({}, "malformed_ballot"),
            ({"ranks": {}}, "empty_ballot"),
            ({"ranks": {9: 1}}, "unknown_option"),
            ({"ranks": {1: 1, "1": 2}}, "duplicate_option"),
            ({"ranks": {1: 0}}, "invalid_rank"),
            ({"ranks": {1: 4}}, "invalid_rank"),
            ({"ranks": {1: True}}, "invalid_rank"),
            ({"ranks": {1: "1"}}, "invalid_rank"),
        ],
    )
    def test_rejects_invalid_ballots(self, content: dict[str, object], code: str) -> None:
        with pytest.raises(InvalidBallotError) as error:
            validate_ballot(spec(), content)
        assert error.value.code == code


class TestTally:
    def test_wikipedia_example(self) -> None:
        """The 45 voters of the Schulze method article (Wikipedia): E > A > C > B > D, and no
        Condorcet winner."""
        poll = spec((A, B, C, D, E))
        ballots = ranked(
            poll,
            (5, "ACBED"),
            (5, "ADECB"),
            (8, "BEDAC"),
            (3, "CABED"),
            (7, "CAEBD"),
            (2, "CBADE"),
            (7, "DCEBA"),
            (8, "EBADC"),
        )
        outcome = outcome_of(poll, ballots)
        assert outcome.status is Status.DECIDED
        assert outcome.ranking == ((E,), (A,), (C,), (B,), (D,))
        assert outcome.winner == E
        assert outcome.condorcet_winner is None
        assert outcome.pairwise == (
            (0, 20, 26, 30, 22),
            (25, 0, 16, 33, 18),
            (19, 29, 0, 17, 24),
            (15, 12, 28, 0, 14),
            (23, 27, 21, 31, 0),
        )
        assert outcome.strongest_paths == (
            (0, 28, 28, 30, 24),
            (25, 0, 28, 33, 24),
            (25, 29, 0, 29, 24),
            (25, 28, 28, 0, 24),
            (25, 28, 28, 31, 0),
        )
        wins = {row.option: (row.wins, row.losses) for row in outcome.options}
        assert wins == {A: (2, 2), B: (2, 2), C: (2, 2), D: (1, 3), E: (3, 1)}

    def test_cycle_and_condorcet_winner(self) -> None:
        poll = spec()
        outcome = outcome_of(poll, ranked(poll, (4, "BAC"), (3, "ACB"), (2, "CBA")))
        # B beats A 6 to 3, A beats C 7 to 2, C beats B 5 to 4: a cycle, where the weakest
        # duel (C over B) gives way.
        assert outcome.pairwise == ((0, 3, 7), (6, 0, 4), (2, 5, 0))
        assert outcome.condorcet_winner is None
        assert outcome.ranking == ((B,), (A,), (C,))

        outcome = outcome_of(poll, ranked(poll, (4, "BAC"), (3, "ACB"), (2, "BCA")))
        assert outcome.condorcet_winner == B
        assert outcome.winner == B
        assert outcome.status is Status.DECIDED

    def test_a_perfect_cycle_is_a_tie(self) -> None:
        poll = spec()
        outcome = outcome_of(poll, ranked(poll, (1, "ABC"), (1, "BCA"), (1, "CAB")))
        assert outcome.status is Status.TIE
        assert outcome.winner is None
        assert outcome.tied == (A, B, C)
        assert outcome.ranking == ((A, B, C),)
        assert outcome.condorcet_winner is None

    def test_tied_duel_at_the_top(self) -> None:
        poll = spec()
        outcome = outcome_of(poll, ranked(poll, (1, "ABC"), (1, "BAC")))
        assert outcome.status is Status.TIE
        assert outcome.tied == (A, B)
        assert outcome.ranking == ((A, B), (C,))
        assert {row.option: (row.wins, row.losses) for row in outcome.options} == {
            A: (1, 0),
            B: (1, 0),
            C: (0, 2),
        }

    def test_unranked_options_tie_below_the_ranked_ones(self) -> None:
        poll = spec((A, B, C, D))
        ballots = [Ballot(validate_ballot(poll, {"ranks": {"2": 1}}))] * 3
        outcome = outcome_of(poll, ballots)
        assert outcome.ranking == ((B,), (A, C, D))
        assert outcome.condorcet_winner == B
        assert outcome.pairwise[1] == (3, 0, 3, 3)
        assert outcome.pairwise[0] == (0, 0, 0, 0)

    def test_blank_ballots_are_left_out(self) -> None:
        poll = spec()
        result = tally(poll, [*ranked(poll, (2, "CAB")), Ballot(), Ballot()])
        assert (result.counts.cast, result.counts.blank, result.counts.valid) == (4, 2, 2)
        assert result.outcome.winner == C
        assert result.outcome.pairwise[2] == (2, 2, 0)

    def test_no_valid_votes(self) -> None:
        outcome = outcome_of(spec(), [Ballot()])
        assert outcome.status is Status.NO_VALID_VOTES
        assert outcome.ranking == ()
        assert outcome.winner is None
        assert outcome.tied == ()
        assert outcome.condorcet_winner is None
        assert outcome.pairwise == ((0, 0, 0), (0, 0, 0), (0, 0, 0))
        assert [(row.wins, row.losses) for row in outcome.options] == [(0, 0)] * 3

    def test_every_ballot_is_validated_again(self) -> None:
        with pytest.raises(InvalidBallotError, match="invalid_rank"):
            tally(spec(), [Ballot({"ranks": {"1": 9}})])

    def test_result_is_jsonable(self) -> None:
        poll = spec((A, B))
        data = to_jsonable(tally(poll, ranked(poll, (1, "BA"))))
        assert data["outcome"] == {
            "status": "decided",
            "options": [
                {"option": A, "wins": 0, "losses": 1},
                {"option": B, "wins": 1, "losses": 0},
            ],
            "pairwise": [[0, 0], [1, 0]],
            "strongest_paths": [[0, 0], [1, 0]],
            "ranking": [[B], [A]],
            "winner": B,
            "tied": [],
            "condorcet_winner": B,
        }
