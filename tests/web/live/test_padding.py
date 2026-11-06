"""Ballot steps of one size per poll (SPEC §9.2, ``agaura.web.live.padding``): whatever the
choice and the step, the answers of a poll have the same length, and so have the requests
live.js sends."""

from __future__ import annotations

import random
import re
from collections.abc import Iterator
from itertools import chain, combinations, product
from typing import Any
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agaura.models import Poll, User
from agaura.services import assemblies, live
from agaura.services.polls import PollInput
from agaura.web.labels import grade_label, rank_label
from agaura.web.live.ballot_forms import Choice, number_field, rank_name
from agaura.web.live.padding import (
    PAD_FIELD,
    _extremes,
    _section_renderer,
    padded_section,
    request_size,
    section_size,
)
from agaura.web.live.routes import VOTE_RECORDED
from agaura.web.messages import error_message
from agaura.web.security import CSRF_COOKIE
from tests.conftest import RESOLUTION, World
from tests.web.conftest import csrf_token, post
from tests.web.live.conftest import APPROVAL, CONDORCET, MAJORITY_JUDGMENT, ballots_of, join

FETCH = {"X-Requested-With": "fetch"}
CSRF = "c" * 43  # secrets.token_urlsafe(32)
BALLOT_ERRORS = (
    "malformed_ballot",
    "unknown_option",
    "empty_ballot",
    "duplicate_option",
    "too_many_choices",
    "invalid_grade",
    "invalid_rank",
    "live_no_grade",
    "live_no_rank",
    "live_blank_with_choice",
)

SINGLE = PollInput(
    title="Motion",
    method="single_choice",
    options=["Pour", "Contre l'avis & la motion", "Abstention éclairée"],
    blank_label="Ne se prononce pas",
)
SINGLE_MANY = PollInput(
    title="Lieu", method="single_choice", options=[f"Salle {n}" for n in range(1, 13)]
)
APPROVAL_TINY = PollInput(  # a long blank label: the blank summaries are the longest
    title="Bureau",
    method="approval",
    options=["A", "B"],
    blank_label="Je ne soutiens aucune des personnes candidates à cette élection",
)
APPROVAL_FREE = PollInput(
    title="Commissions", method="approval", options=["A", "Bruno Lefèvre", "Chloé <cheffe>", "D"]
)
JUDGMENT_10 = PollInput(
    title="Projets",
    method="majority_judgment",
    options=["Jardin", "Fresque murale", "Banc"],
    params={"grade_count": 10},
)
JUDGMENT_MANY = PollInput(
    title="Idées",
    method="majority_judgment",
    options=[f"Idée {n}" if n % 3 else f"Idée numéro {n}" for n in range(1, 12)],
)
# Labels of one to many bytes at positions of one and two digits: the warning of the summary
# names the options left without a grade, and short labels make that warning the shortest.
JUDGMENT_UNEVEN = PollInput(
    title="Budget",
    method="majority_judgment",
    options=["A", "Fresque murale du préau", "B", "C", "Jardin", "D", "E", "F", "G", "H", "Î", "J"],
)

CONDORCET_TINY = PollInput(  # a long blank label: the blank summaries are the longest
    title="Nom",
    method="condorcet",
    options=["A", "B"],
    blank_label="Je ne veux classer aucune des propositions de nom soumises au vote",
)
# Labels of one to many bytes at positions of one and two digits, as for Majority Judgment: the
# options left unranked save the hidden field of their rank, which depends on their position.
CONDORCET_UNEVEN = PollInput(
    title="Logo",
    method="condorcet",
    options=["A", "Arbre et soleil levant", "B", "C", "D", "E", "F", "G", "H", "Î", "J", "K"],
)


# --- Every choice of small polls, rendered section by section ------------------------------------


def subsets(items: list[int]) -> Iterator[tuple[int, ...]]:
    return chain.from_iterable(combinations(items, size) for size in range(len(items) + 1))


def choices(poll: Poll, *, sample: int | None = None) -> Iterator[Choice]:
    """Every choice a voter can make (and a few the form refuses), or a random sample."""
    method = poll.method
    positions = [option.position for option in poll.options]
    yield Choice(method)
    if method == "single_choice":
        for position in positions:
            yield Choice(method, {"choice": position})
    elif method == "approval":
        for subset in subsets(positions):
            if subset:
                yield Choice(method, {"approved": list(subset)})
            else:
                yield Choice(method, implicit_blank=True)
    elif method == "condorcet":
        ranks: list[int | None] = [None, *range(1, len(positions) + 1)]
        if sample is None:
            orders: Iterator[tuple[int | None, ...]] = product(ranks, repeat=len(positions))
        else:
            rng = random.Random(1)  # noqa: S311 (a reproducible sample)
            orders = iter(
                [tuple(rng.choice(ranks) for _ in positions) for _ in range(sample)]
                + [tuple(rank for _ in positions) for rank in ranks]
                + [tuple(1 if p < 10 else None for p in positions)]
                + [tuple(None if p < 10 else len(positions) for p in positions)]
            )
        for order in orders:
            given = {str(p): r for p, r in zip(positions, order, strict=True) if r is not None}
            yield Choice(method, {"ranks": given})
    else:
        count = poll.params.get("grade_count", 6)
        values = [None, *range(count)]
        if sample is None:
            combos: Iterator[tuple[int | None, ...]] = product(values, repeat=len(positions))
        else:
            rng = random.Random(1)  # noqa: S311 (a reproducible sample)
            combos = iter(
                [tuple(rng.choice(values) for _ in positions) for _ in range(sample)]
                + [tuple(grade for _ in positions) for grade in values]
                + [tuple(0 if p < 10 else None for p in positions)]
                + [tuple(None if p < 10 else count - 1 for p in positions)]
            )
        for combo in combos:
            grades = {str(p): g for p, g in zip(positions, combo, strict=True) if g is not None}
            yield Choice(method, {"grades": grades})


def load(world: World, public_id: str, poll_id: int) -> Poll:
    with world.database.read() as db:
        assembly = assemblies.find_assembly(db, public_id)
        assert assembly is not None
        poll = live.voter_poll(db, assembly.id, poll_id)
        assert poll is not None
        return poll


@pytest.mark.parametrize(
    ("data", "sample"),
    [
        (RESOLUTION, None),
        (SINGLE, None),
        (SINGLE_MANY, None),
        (APPROVAL, None),
        (APPROVAL_TINY, None),
        (APPROVAL_FREE, None),
        (MAJORITY_JUDGMENT, None),
        (JUDGMENT_10, None),
        (JUDGMENT_MANY, 300),
        (CONDORCET, None),
        (CONDORCET_TINY, None),
        (CONDORCET_UNEVEN, 300),
    ],
    ids=lambda value: value.title if isinstance(value, PollInput) else str(value),
)
@pytest.mark.parametrize("focus", [False, True], ids=["page", "fragment"])
def test_every_step_of_every_choice_has_the_same_size(
    world: World, owner: User, data: PollInput, sample: int | None, focus: bool
) -> None:
    """No section of a ballot step is longer than ``section_size``, so padding brings every
    one of them to the same size."""
    public_id = world.assembly(owner)
    poll = load(world, public_id, world.poll(owner, public_id, data))
    target = section_size(public_id, CSRF, poll, focus=focus)
    render = _section_renderer(public_id, CSRF, poll, focus)
    errors = [error_message(code) for code in BALLOT_ERRORS]
    longest_error = max(errors, key=lambda text: len(text.encode()))
    messages = [VOTE_RECORDED, error_message("already_voted"), error_message("poll_not_open")]

    def size(state: str, choice: Choice | None = None, **kwargs: Any) -> int:
        return len(render(state, choice, **kwargs).encode())

    sizes = [size("voted", message=message) for message in messages]
    sizes += [size("closed", message=message) for message in messages]
    sizes += [size("ballot", error=error) for error in [None, *errors]]
    # The extremes alone, without the longest message's margin, bound every choice.
    bounds = {state: 0 for state in ("review", "ballot")}
    for state, choice in _extremes(poll):
        if state in bounds:
            bounds[state] = max(bounds[state], size(state, choice))
    for choice in choices(poll, sample=sample):
        assert size("review", choice) <= bounds["review"], choice
        assert size("ballot", choice) <= bounds["ballot"], choice
        sizes.append(size("ballot", choice, error=longest_error))
    assert max(sizes + list(bounds.values())) <= target

    padded = {
        len(padded_section(public_id, CSRF, poll, state, focus=focus, **kwargs).encode())
        for state, kwargs in [
            ("voted", {}),
            ("voted", {"message": error_message("already_voted")}),
            ("ballot", {}),
            ("review", {"choice": Choice(poll.method)}),
        ]
    }
    assert padded == {target + len("<!---->")}


@pytest.mark.parametrize("data", [JUDGMENT_UNEVEN, JUDGMENT_10], ids=str)
def test_every_set_of_ungraded_options_fits(world: World, owner: User, data: PollInput) -> None:
    """Every set of options a Majority Judgment summary leaves ungraded (and names in its
    warning), the others at their longest grade, fits the bound of ``_extremes``."""
    public_id = world.assembly(owner)
    poll = load(world, public_id, world.poll(owner, public_id, data))
    render = _section_renderer(public_id, CSRF, poll, False)

    def size(choice: Choice) -> int:
        return len(render("review", choice).encode())

    bound = max(size(choice) for state, choice in _extremes(poll) if state == "review" and choice)
    count = poll.params.get("grade_count", 6)
    best = max(range(count), key=lambda grade: len(grade_label(grade, count).encode()))
    positions = [option.position for option in poll.options]
    for graded in subsets(positions):
        choice = Choice(poll.method, {"grades": {str(p): best for p in graded}})
        assert size(choice) <= bound, graded
    assert "pas de mention" in render("review", Choice(poll.method, {"grades": {"1": best}}))


def test_every_set_of_unranked_options_fits(world: World, owner: User) -> None:
    """Every set of options a Condorcet summary leaves unranked, the others at the longest
    rank, fits the bound of ``_extremes``; so does every rank given to all options."""
    public_id = world.assembly(owner)
    poll = load(world, public_id, world.poll(owner, public_id, CONDORCET_UNEVEN))
    render = _section_renderer(public_id, CSRF, poll, False)

    def size(choice: Choice) -> int:
        return len(render("review", choice).encode())

    bound = max(size(choice) for state, choice in _extremes(poll) if state == "review" and choice)
    positions = [option.position for option in poll.options]
    longest = max(
        range(1, len(positions) + 1),
        key=lambda rank: len(rank_label(rank).encode()) + len(str(rank)),
    )
    for ranked in subsets(positions):
        choice = Choice(poll.method, {"ranks": {str(p): longest for p in ranked}})
        assert size(choice) <= bound, ranked
    for rank in range(1, len(positions) + 1):
        assert (
            size(Choice(poll.method, {"ranks": dict.fromkeys(map(str, positions), rank)})) <= bound
        )
    assert "non classées" in render("review", Choice(poll.method, {"ranks": {"1": 1}}))


# --- Through the routes ------------------------------------------------------------------------

STEPS = {
    "single_choice": [
        {"choice": "1"},
        {"choice": "2"},
        {"choice": "00"},
        {"choice": "1", "blank": "1"},  # only by hand: refused
    ],
    "approval": [
        {},  # nothing checked: a blank vote
        {"blank": "1"},
        {"approved": ["1"]},
        {"approved": ["2", "3"]},
        {"approved": ["1", "2", "3"]},  # too many: the ballot comes back with an error
        {"approved": ["²"]},  # unreadable
        {"approved": ["1", "2"], "blank": "1"},  # blank with options: refused, options kept
    ],
    "majority_judgment": [
        {"blank": "1"},
        {"grade-01": "0"},
        {"grade-01": "5", "grade-02": "3", "grade-03": "0"},
        {},  # nothing graded: the ballot comes back with an error
        {"grade-02": "1", "blank": "1"},  # blank with a grade: refused, the grade kept
    ],
    "condorcet": [
        {"blank": "1", "rank-01": "", "rank-02": "", "rank-03": "", "rank-04": ""},
        {"rank-01": "1", "rank-02": "", "rank-03": "", "rank-04": ""},
        {"rank-01": "4", "rank-02": "3", "rank-03": "2", "rank-04": "1"},
        {"rank-01": "2", "rank-02": "2", "rank-03": "2", "rank-04": "2"},
        {"rank-01": "", "rank-02": "", "rank-03": "", "rank-04": ""},  # nothing ranked: an error
        {"rank-02": "1", "blank": "1"},  # blank with a rank: refused, the rank kept
    ],
}
POLLS = {
    "single_choice": RESOLUTION,
    "approval": APPROVAL,
    "majority_judgment": MAJORITY_JUDGMENT,
    "condorcet": CONDORCET,
}


def voters(app: FastAPI, world: World, owner: User, public_id: str, count: int):
    """Phones that joined the session. No ``with``: ``client`` started the application."""
    for _ in range(count):
        phone = TestClient(app, follow_redirects=False)
        join(phone, world, owner, public_id)
        yield phone


@pytest.mark.parametrize("method", list(STEPS))
def test_answers_of_a_poll_have_one_length(
    client: TestClient, app: FastAPI, world: World, owner: User, method: str
) -> None:
    public_id = world.assembly(owner)
    join(client, world, owner, public_id)
    poll_id = world.poll(owner, public_id, POLLS[method])
    url = f"/{public_id}/polls/{poll_id}"
    lengths: dict[str, set[int]] = {"page": set(), "fragment": set()}
    for mode, headers in (("page", {}), ("fragment", FETCH)):
        for data in STEPS[method]:
            for step in ({}, {"action": "edit"}):
                response = post(client, f"{url}/review", {**data, **step}, headers=headers)
                assert response.status_code in (200, 422)
                lengths[mode].add(len(response.content))
    # Casting: one phone per choice; refusals (422) are as long as the vote recorded.
    phones = voters(app, world, owner, public_id, len(STEPS[method]))
    for data, phone in zip(STEPS[method], phones, strict=True):
        response = post(phone, url, data, headers=FETCH)
        assert response.status_code in (200, 422)
        lengths["fragment"].add(len(response.content))
        if response.status_code == 422:
            lengths["page"].add(len(post(phone, url, data).content))
    assert len(lengths["page"]) == 1 and len(lengths["fragment"]) == 1, lengths
    assert len(ballots_of(world, poll_id)) >= 2


def test_the_next_vote_never_changes_an_answer(
    client: TestClient, app: FastAPI, world: World, owner: User
) -> None:
    """ "Vote suivant" leads to another open poll: live.js writes its target from the summary of
    the open polls, so the answer of a vote is the same whatever the polls still to vote."""
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, APPROVAL)
    answers = set()
    for title in ("Z", "Élection de la commission des fêtes et de la vie associative"):
        world.poll(owner, public_id, PollInput(title, "single_choice", ["Oui", "Non"]))
        phone = next(voters(app, world, owner, public_id, 1))
        response = post(phone, f"/{public_id}/polls/{poll_id}", {"approved": ["1"]}, headers=FETCH)
        assert response.status_code == 200 and "data-next-vote hidden" in response.text
        assert "commission" not in response.text
        answers.add(response.content)
    assert len(answers) == 1


def test_answers_are_never_compressed_by_the_application(
    client: TestClient, world: World, owner: User, session_id: str
) -> None:
    join(client, world, owner, session_id)
    poll_id = world.poll(owner, session_id)
    response = post(
        client,
        f"/{session_id}/polls/{poll_id}/review",
        {"choice": "1"},
        headers={**FETCH, "Accept-Encoding": "gzip, br, zstd"},
    )
    assert "content-encoding" not in response.headers


# --- Requests -----------------------------------------------------------------------------------


def data_pad(html: str) -> set[int]:
    return {int(value) for value in re.findall(r'data-pad="(\d+)"', html)}


@pytest.mark.parametrize(
    "data",
    [RESOLUTION, SINGLE_MANY, APPROVAL, MAJORITY_JUDGMENT, JUDGMENT_10, CONDORCET_UNEVEN],
    ids=str,
)
def test_every_request_fits_the_size_forms_announce(
    client: TestClient, world: World, owner: User, data: PollInput
) -> None:
    """live.js adds ``pad`` up to ``data-pad``: every body it can send must fit."""
    public_id = world.assembly(owner)
    join(client, world, owner, public_id)
    poll_id = world.poll(owner, public_id, data)
    token = csrf_token(client)
    poll = load(world, public_id, poll_id)
    size = request_size(poll, token)
    assert data_pad(client.get(f"/{public_id}").text) == {size}  # the ballot form
    blank = {"choice": "00", "blank": "1"}
    summary = post(client, f"/{public_id}/polls/{poll_id}/review", blank)
    assert data_pad(summary.text) == {size}  # the summary form
    for choice in choices(poll, sample=50):
        for button in ([], [("blank", "1")], [("action", "edit")]):
            fields = [("csrf_token", token), *choice.fields(poll), *button]
            assert len(urlencode(fields)) + len(f"&{PAD_FIELD}=") <= size
    if poll.method == "condorcet":
        # The ballot form sends every list, "Non classée" as long as a rank.
        last = number_field(len(poll.options))
        lists = [(rank_name(option.position), last) for option in poll.options]
        fields = [("csrf_token", token), *lists, ("blank", "1")]
        assert len(urlencode(fields)) + len(f"&{PAD_FIELD}=") <= size


def test_the_server_ignores_the_padding(
    client: TestClient, world: World, owner: User, session_id: str
) -> None:
    join(client, world, owner, session_id)
    poll_id = world.poll(owner, session_id, APPROVAL)
    url = f"/{session_id}/polls/{poll_id}"
    headers = {**FETCH, "X-CSRF-Token": client.cookies[CSRF_COOKIE]}
    plain = client.post(f"{url}/review", data={"approved": ["2"]}, headers=headers)
    padded = client.post(
        f"{url}/review", data={"approved": ["2"], PAD_FIELD: "x" * 40}, headers=headers
    )
    assert padded.status_code == 200 and padded.text == plain.text  # not echoed, not refused
    done = client.post(url, data={"approved": ["2"], PAD_FIELD: "x" * 37}, headers=headers)
    assert done.status_code == 200 and "Vote enregistré" in done.text
    assert ballots_of(world, poll_id) == [(False, {"approved": [2]})]


def form_values(section: str, name_pattern: str) -> dict[str, set[str]]:
    """The values each field of a ballot form can take, as the HTML offers them."""
    values: dict[str, set[str]] = {}
    for name, value in re.findall(rf'name="({name_pattern})" value="([^"]*)"', section):
        values.setdefault(name, set()).add(value)
    for name, body in re.findall(
        rf'<select [^>]*name="({name_pattern})">(.*?)</select>', section, re.S
    ):
        values.setdefault(name, set()).update(re.findall(r'<option value="([^"]*)"', body))
    return values


@pytest.mark.parametrize(
    ("data", "pattern"),
    [(SINGLE_MANY, "choice"), (CONDORCET_UNEVEN, r"rank-\d+"), (APPROVAL, "approved")],
    ids=["single_choice", "condorcet", "approval"],
)
def test_without_javascript_every_choice_sends_one_size(
    client: TestClient, world: World, owner: User, data: PollInput, pattern: str
) -> None:
    """A browser without JavaScript sends the fields as they are (SPEC §9.2): every value a
    field can take has one length, the blank radio button of single choice and "Non classée"
    of Condorcet included, and so does every field name. ``choice=7`` and ``choice=12`` would
    otherwise tell options 1 to 9 from options 10 to 30."""
    public_id = world.assembly(owner)
    join(client, world, owner, public_id)
    poll_id = world.poll(owner, public_id, data)
    page = client.get(f"/{public_id}").text
    start = page.index(f'id="poll-{poll_id}"')
    values = form_values(page[start : page.index("</form>", start)], pattern)
    assert values and len({len(name) for name in values}) == 1
    lengths = {len(value) for offered in values.values() for value in offered}
    assert lengths == {2}, values
    if data.method == "condorcet":  # every list is sent: one body for every ranking
        assert all("00" in offered for offered in values.values())
