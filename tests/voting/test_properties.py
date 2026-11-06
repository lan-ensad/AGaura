"""Property-based tests: invariants that must hold for any set of ballots."""

import statistics

from hypothesis import assume, given
from hypothesis import strategies as st

from agaura.voting import Ballot, PollSpec, tally, to_jsonable, validate_ballot

option_sets = st.lists(st.integers(1, 1000), min_size=2, max_size=6, unique=True).map(tuple)


@st.composite
def single_choice_polls(draw: st.DrawFn) -> tuple[PollSpec, list[Ballot]]:
    options = draw(option_sets)
    choices = draw(st.lists(st.one_of(st.none(), st.sampled_from(options)), max_size=40))
    ballots = [Ballot(None if c is None else {"choice": c}) for c in choices]
    return PollSpec("single_choice", options), ballots


@st.composite
def approval_polls(draw: st.DrawFn) -> tuple[PollSpec, list[Ballot]]:
    options = draw(option_sets)
    seats = draw(st.integers(1, len(options)))
    groups = draw(st.lists(st.sets(st.sampled_from(options), min_size=1), max_size=40))
    ballots = [Ballot({"approved": sorted(group)}) for group in groups]
    return PollSpec("approval", options, {"seats": seats}), ballots


@st.composite
def judgment_polls(draw: st.DrawFn) -> tuple[PollSpec, list[Ballot]]:
    options = draw(option_sets)
    grade_count = draw(st.integers(2, 7))
    grade = st.integers(0, grade_count - 1)
    rows = draw(st.lists(st.fixed_dictionaries({str(o): grade for o in options}), max_size=40))
    ballots = [Ballot({"grades": row}) for row in rows]
    return PollSpec("majority_judgment", options, {"grade_count": grade_count}), ballots


@st.composite
def condorcet_polls(draw: st.DrawFn) -> tuple[PollSpec, list[Ballot]]:
    """Ranks as typed (ties, gaps, options left unranked), then normalized as when cast."""
    options = draw(option_sets)
    spec = PollSpec("condorcet", options)
    rank = st.one_of(st.none(), st.integers(1, len(options)))
    rows = draw(st.lists(st.fixed_dictionaries({str(o): rank for o in options}), max_size=40))
    typed = [{key: value for key, value in row.items() if value is not None} for row in rows]
    ballots = [Ballot(validate_ballot(spec, {"ranks": ranks})) for ranks in typed if ranks]
    return spec, ballots


any_poll = st.one_of(single_choice_polls(), approval_polls(), judgment_polls(), condorcet_polls())


@given(any_poll, st.data())
def test_result_does_not_depend_on_ballot_order(
    poll: tuple[PollSpec, list[Ballot]], data: st.DataObject
) -> None:
    spec, ballots = poll
    shuffled = data.draw(st.permutations(ballots))
    assert to_jsonable(tally(spec, shuffled)) == to_jsonable(tally(spec, ballots))


@given(single_choice_polls())
def test_single_choice_votes_add_up_and_winner_leads(poll: tuple[PollSpec, list[Ballot]]) -> None:
    spec, ballots = poll
    result = tally(spec, ballots)
    votes = {o.option: o.votes for o in result.outcome.options}
    assert sum(votes.values()) == result.counts.valid
    winner = result.outcome.winner
    if winner is not None:
        assert all(votes[winner] > v for o, v in votes.items() if o != winner)


@given(approval_polls())
def test_approval_invariants(poll: tuple[PollSpec, list[Ballot]]) -> None:
    spec, ballots = poll
    outcome = tally(spec, ballots).outcome
    approvals = {o.option: o.approvals for o in outcome.options}
    assert sum(approvals.values()) == sum(len(b.content["approved"]) for b in ballots)
    assert len(outcome.elected) + outcome.seats_in_tie + outcome.unfilled_seats == outcome.seats
    others = [o for o in spec.options if o not in outcome.elected]
    for elected in outcome.elected:
        assert all(approvals[elected] >= approvals[o] for o in others)
        assert approvals[elected] > 0


@given(judgment_polls())
def test_majority_grade_is_the_lower_median(poll: tuple[PollSpec, list[Ballot]]) -> None:
    spec, ballots = poll
    assume(ballots)
    outcome = tally(spec, ballots).outcome
    for tallied in outcome.options:
        grades = [b.content["grades"][str(tallied.option)] for b in ballots]
        # Grade 0 is the best, so the "lower" (worse) median is the higher index.
        assert tallied.majority_grade == statistics.median_high(grades)


@given(
    st.integers(2, 7).flatmap(
        lambda k: st.tuples(
            st.just(k),
            st.lists(
                st.tuples(st.integers(0, k - 1), st.integers(0, k - 1)).map(sorted),
                min_size=1,
                max_size=40,
            ),
        )
    )
)
def test_majority_judgment_respects_unanimous_dominance(
    case: tuple[int, list[list[int]]],
) -> None:
    """If every voter grades A at least as well as B, and one strictly better, A ranks above B."""
    grade_count, pairs = case
    assume(any(a != b for a, b in pairs))
    a_id, b_id = 1, 2
    ballots = [Ballot({"grades": {str(a_id): a, str(b_id): b}}) for a, b in pairs]
    spec = PollSpec("majority_judgment", (a_id, b_id), {"grade_count": grade_count})
    ranking = tally(spec, ballots).outcome.ranking
    assert ranking == ((a_id,), (b_id,))


@given(condorcet_polls())
def test_condorcet_ranking_follows_the_strongest_paths(
    poll: tuple[PollSpec, list[Ballot]],
) -> None:
    """Every option appears once; an option ranks above another exactly when its strongest
    path to it is stronger than the reverse, and options of one group have equal paths."""
    spec, ballots = poll
    assume(ballots)
    outcome = tally(spec, ballots).outcome
    index = {option: i for i, option in enumerate(spec.options)}
    paths = outcome.strongest_paths
    order = [option for group in outcome.ranking for option in group]
    assert sorted(order) == sorted(spec.options)
    level = {option: n for n, group in enumerate(outcome.ranking) for option in group}
    for a in spec.options:
        for b in spec.options:
            i, j = index[a], index[b]
            if level[a] == level[b]:
                assert paths[i][j] == paths[j][i]
            if paths[i][j] > paths[j][i]:
                assert level[a] < level[b]
    # Duels add up: each pair of options is compared on every valid ballot, once.
    for i in range(len(spec.options)):
        for j in range(len(spec.options)):
            if i != j:
                assert outcome.pairwise[i][j] + outcome.pairwise[j][i] <= len(ballots)


@given(condorcet_polls())
def test_condorcet_winner_always_wins(poll: tuple[PollSpec, list[Ballot]]) -> None:
    spec, ballots = poll
    assume(ballots)
    outcome = tally(spec, ballots).outcome
    size = len(spec.options)
    beats_all = [
        option
        for i, option in enumerate(spec.options)
        if all(outcome.pairwise[i][j] > outcome.pairwise[j][i] for j in range(size) if j != i)
    ]
    assert outcome.condorcet_winner == (beats_all[0] if beats_all else None)
    if beats_all:
        assert outcome.winner == beats_all[0]
        assert outcome.ranking[0] == (beats_all[0],)
