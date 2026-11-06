"""Ballots as CSV (FR-60): one row per ballot, random order, nothing that identifies a voter."""

from __future__ import annotations

import csv
import io
import random
import re
from collections import Counter

import pytest

from agaura.exports import csv_export
from agaura.models import Ballot, User
from agaura.services import exports, polls
from agaura.services.exports import PollRecord
from agaura.services.polls import PollInput
from tests.conftest import World
from tests.exports.conftest import CONDORCET, CONDORCET_VOTES, INJECTION, Built, vote_all

BOM = b"\xef\xbb\xbf"


def record(world: World, owner: User, public_id: str, poll_id: int) -> PollRecord:
    with world.database.read() as db:
        return exports.poll_record(db, owner, public_id, poll_id)[1]


def parse(content: bytes) -> list[list[str]]:
    assert content.startswith(BOM)
    text = content[len(BOM) :].decode()
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
    return list(csv.reader(io.StringIO(text, newline=""), delimiter=";"))


def closed_poll(world: World, owner: User, data: PollInput, votes: list) -> tuple[str, int]:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, data)
    vote_all(world, public_id, world.voters(owner, public_id, len(votes)), poll_id, votes)
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
    return public_id, poll_id


def test_single_choice(world: World, owner: User, built: Built) -> None:
    header, *rows = parse(
        csv_export.ballots_csv(record(world, owner, built.public_id, built.polls["resolution"]))
    )
    assert header == ["Pour", "Contre", "Abstention"]
    assert Counter(map(tuple, rows)) == {("1", "0", "0"): 3, ("0", "1", "0"): 2, ("0", "0", "1"): 1}


def test_approval(world: World, owner: User, built: Built) -> None:
    header, *rows = parse(
        csv_export.ballots_csv(record(world, owner, built.public_id, built.polls["approval"]))
    )
    assert header == ["Alice", "Bruno", "Chloé", "David", "Vote blanc"]
    assert Counter(map(tuple, rows)) == {
        ("1", "1", "0", "0", "0"): 1,
        ("1", "0", "1", "0", "0"): 1,
        ("1", "0", "0", "0", "0"): 1,
        ("0", "0", "0", "0", "1"): 1,
        ("0", "1", "1", "0", "0"): 1,
        ("0", "0", "0", "1", "0"): 1,
    }


def test_majority_judgment_uses_grade_labels(world: World, owner: User, built: Built) -> None:
    header, *rows = parse(
        csv_export.ballots_csv(
            record(world, owner, built.public_id, built.polls["majority_judgment"])
        )
    )
    assert header == ["Jardin", "Fresque", "Vote blanc"]
    assert Counter(map(tuple, rows)) == {
        ("Très bien", "Bien", "0"): 1,
        ("Bien", "Très bien", "0"): 1,
        ("Assez bien", "Assez bien", "0"): 1,
        ("", "", "1"): 1,
        ("Passable", "À rejeter", "0"): 1,  # ungraded option: the worst grade
        ("À rejeter", "Passable", "0"): 1,
    }


def test_condorcet_gives_the_rank_of_each_option(world: World, owner: User) -> None:
    public_id, poll_id = closed_poll(world, owner, CONDORCET, CONDORCET_VOTES)
    header, *rows = parse(csv_export.ballots_csv(record(world, owner, public_id, poll_id)))
    assert header == ["Gazette", "Écho", "Lien", "Vote blanc"]
    assert Counter(map(tuple, rows)) == {
        ("2", "1", "3", "0"): 4,
        ("1", "3", "2", "0"): 3,
        ("3", "2", "1", "0"): 2,  # one of them left Gazette unranked: the last rank
        ("", "", "", "1"): 1,
    }


def test_custom_grade_scale(world: World, owner: User) -> None:
    data = PollInput("Notes", "majority_judgment", ["A", "B"], {"grade_count": 3})
    public_id, poll_id = closed_poll(world, owner, data, [{"grades": {"1": 0, "2": 2}}])
    _, row = parse(csv_export.ballots_csv(record(world, owner, public_id, poll_id)))
    assert row == ["Mention 1", "Mention 3", "0"]


def test_no_identifier_and_no_timestamp(world: World, owner: User, built: Built) -> None:
    for key in ("resolution", "approval", "majority_judgment", "cancelled_open"):
        item = record(world, owner, built.public_id, built.polls[key])
        content = csv_export.ballots_csv(item).decode()
        assert item.ballots
        assert len(parse(content.encode())) == 1 + len(item.ballots)  # one row per ballot
        for ballot in item.ballots:
            assert ballot.id not in content
        assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}", content)


def test_formula_injection_is_neutralized(world: World, owner: User) -> None:
    public_id, poll_id = closed_poll(world, owner, INJECTION, [{"choice": 1}, None])
    header, *rows = parse(csv_export.ballots_csv(record(world, owner, public_id, poll_id)))
    assert header == [
        '\'=HYPERLINK("http://example.org")',
        "'+33 Salle",
        "'-Parc",
        "'@Ferme",
        "Gare",
        "'=1+1",
    ]
    assert sorted(rows) == [["0", "0", "0", "0", "0", "1"], ["1", "0", "0", "0", "0", "0"]]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("=SUM(A1)", "'=SUM(A1)"),
        ("\tx", "'\tx"),
        ("\uff1dSUM(A1)", "'\uff1dSUM(A1)"),
        ("Très bien", "Très bien"),
        ("a=b", "a=b"),
        ("", ""),
    ],
)
def test_safe_cell(text: str, expected: str) -> None:
    assert csv_export.safe_cell(text) == expected


def test_rows_are_shuffled_by_the_system_generator(
    world: World, owner: User, built: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = record(world, owner, built.public_id, built.polls["approval"])
    used = []

    class Recorder(random.Random):
        def shuffle(self, x: list) -> None:  # type: ignore[override]
            used.append(len(x))
            x.reverse()

    monkeypatch.setattr(csv_export.secrets, "SystemRandom", Recorder)
    _, *rows = parse(csv_export.ballots_csv(item))
    assert used == [6]
    in_id_order = [csv_export._row(item.poll, ballot) for ballot in item.ballots or ()]
    assert rows == in_id_order[::-1]


def test_two_exports_differ(world: World, owner: User) -> None:
    rng = random.Random(7)  # noqa: S311 (reproducible test data)
    data = PollInput("Projets", "majority_judgment", ["A", "B", "C"])
    votes = [{"grades": {str(i): rng.randrange(6) for i in (1, 2, 3)}} for _ in range(40)]
    public_id, poll_id = closed_poll(world, owner, data, votes)
    item = record(world, owner, public_id, poll_id)
    first, second = (parse(csv_export.ballots_csv(item))[1:] for _ in range(2))
    assert Counter(map(tuple, first)) == Counter(map(tuple, second))
    assert first != second  # 40 rows: the chance of drawing the same order is negligible


def test_empty_poll(world: World, owner: User) -> None:
    public_id, poll_id = closed_poll(world, owner, PollInput("Q", "approval", ["A", "B"]), [])
    assert parse(csv_export.ballots_csv(record(world, owner, public_id, poll_id))) == [
        ["A", "B", "Vote blanc"]
    ]


def test_blank_ballots_have_no_content(world: World, owner: User, built: Built) -> None:
    item = record(world, owner, built.public_id, built.polls["resolution"])
    blank = [ballot for ballot in item.ballots or () if ballot.is_blank]
    assert len(blank) == 1 and isinstance(blank[0], Ballot) and blank[0].content is None
