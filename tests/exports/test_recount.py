"""Independent recount of a JSON export (FR-63), and its command line ``scripts/recount.py``."""

from __future__ import annotations

import copy
import datetime as dt
import io
import json
import re
import runpy
from pathlib import Path
from typing import Any

import pytest

from agaura.exports import json_export, recount
from agaura.models import User
from agaura.services import exports, polls
from agaura.services.polls import PollInput
from tests.conftest import World
from tests.exports.conftest import CONDORCET, CONDORCET_VOTES, Built, vote_all

SCRIPT = Path(__file__).parents[2] / "scripts" / "recount.py"


def document_of(world: World, owner: User, public_id: str) -> dict[str, Any]:
    with world.database.read() as db:
        session = exports.session_record(db, owner, public_id, with_ballots=True)
        document = json_export.document(session, session.finished, now=dt.datetime.now(dt.UTC))
    return json.loads(json_export.dumps(document))


def run(tmp_path: Path, document: Any) -> tuple[int, str]:
    path = tmp_path / "export.json"
    path.write_text(document if isinstance(document, str) else json.dumps(document))
    out = io.StringIO()
    return recount.main([str(path)], out=out), out.getvalue()


@pytest.fixture
def document(world: World, owner: User, built: Built) -> dict[str, Any]:
    return document_of(world, owner, built.public_id)


def test_every_poll_matches(tmp_path: Path, document: dict[str, Any]) -> None:
    status, output = run(tmp_path, document)
    assert status == 0, output
    assert "Every digest and every result matches." in output
    assert output.count(": OK") == 6
    assert "no stored result (poll cancelled before closing)" in output


def test_attendees_base_is_recounted(tmp_path: Path, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    data = PollInput("Statuts", "single_choice", ["Pour", "Contre"], {"base": "attendees"})
    poll_id = world.poll(owner, public_id, data)
    tokens = world.voters(owner, public_id, 5)
    vote_all(world, public_id, tokens[:3], poll_id, [{"choice": 1}, {"choice": 1}, None])
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
    document = document_of(world, owner, public_id)
    assert document["polls"][0]["result"]["data"]["outcome"]["base_count"] == 5
    assert run(tmp_path, document)[0] == 0


def test_condorcet_is_recounted(tmp_path: Path, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, CONDORCET)
    tokens = world.voters(owner, public_id, len(CONDORCET_VOTES))
    vote_all(world, public_id, tokens, poll_id, CONDORCET_VOTES)
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
    document = document_of(world, owner, public_id)
    (entry,) = document["polls"]
    outcome = entry["result"]["data"]["outcome"]
    assert outcome["ranking"] == [[2], [1], [3]] and outcome["condorcet_winner"] is None
    assert run(tmp_path, document)[0] == 0
    # A ballot changed in the file to a content no ballot has (all three level): another
    # digest, and another result.
    first_vote(entry).update(content={"ranks": {"1": 1, "2": 1, "3": 1}})
    status, output = run(tmp_path, document)
    assert status == 1 and "digest of the ballots differs" in output


def first_vote(entry: dict[str, Any]) -> dict[str, Any]:
    """The first ballot that is not blank (ballot ids are random)."""
    return next(ballot for ballot in entry["ballots"] if not ballot["is_blank"])


def first_choice(entry: dict[str, Any]) -> int:
    return first_vote(entry)["content"]["choice"]


@pytest.mark.parametrize(
    ("change", "problems"),
    [
        (
            lambda entry: first_vote(entry).update(content={"choice": 3 - first_choice(entry)}),
            ["digest of the ballots differs", "recomputed result differs"],
        ),
        (
            lambda entry: entry["result"]["data"]["counts"].update(cast=7),
            ["recomputed result differs"],
        ),
        (
            lambda entry: entry["ballots"].pop(),
            ["participations but 5 ballots", "digest of the ballots differs"],
        ),
        (lambda entry: entry["ballots"].reverse(), ["not sorted by id", "digest"]),
        (
            lambda entry: entry["ballots"].append(dict(entry["ballots"][-1])),
            ["same id"],
        ),
        (lambda entry: first_vote(entry).update(is_blank=True), ["marked blank"]),
        (lambda entry: entry["definition"].update(title="Autre"), ["digest of the ballots"]),
        (
            lambda entry: first_vote(entry).update(content={"choice": 9}),
            ["cannot be tallied: unknown_option"],
        ),
        (lambda entry: entry.update(result=None), ["no stored result"]),
    ],
)
def test_tampering_is_detected(
    tmp_path: Path, document: dict[str, Any], change: Any, problems: list[str]
) -> None:
    changed = copy.deepcopy(document)
    change(changed["polls"][0])
    status, output = run(tmp_path, changed)
    assert status == 1
    assert "1 poll(s) do not match." in output and "MISMATCH" in output
    for problem in problems:
        assert problem in output


def test_a_forged_title_cannot_hide_lines_of_the_report(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    """ESC[8m conceals what follows in a terminal, a bidi override reorders it: a forged export
    could print a fake "OK" over its own MISMATCH. Such characters come out as escapes."""
    changed = copy.deepcopy(document)
    changed["session"]["title"] = "AG\x1b[8m\u202e"
    changed["polls"][0]["result"]["ballots_sha256"] = "\x1b[2K\r  #1 OK"
    changed["polls"][0]["definition"]["title"] = "Budget\x1b[1A\x1b[2K: OK\u2028"
    status, output = run(tmp_path, changed)
    assert status == 1
    assert "\x1b" not in output and "\u202e" not in output and "\u2028" not in output
    assert "\\u001b[8m" in output and "\\u202e" in output and "\\u2028" in output
    assert "MISMATCH" in output


def test_report_keeps_accents_and_no_break_spaces() -> None:
    text = "\u00c9lection du bureau\u00a0: vote n\u00b0\u202f1"
    assert recount.printable(text) == text


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not json", "Expecting value"),
        ({"format": "other"}, "not an AGaura export"),
        ({"format": "agaura-export", "version": 2}, "unsupported export version"),
        ({"format": "agaura-export", "version": 1}, "malformed export"),
        ({"format": "agaura-export", "version": 1, "polls": [{"ballots": []}]}, "malformed"),
    ],
)
def test_unreadable_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: Any, message: str
) -> None:
    status, _ = run(tmp_path, content)
    assert status == 2
    assert message in capsys.readouterr().err


def test_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert recount.main([str(tmp_path / "absent.json")]) == 2
    assert "absent.json" in capsys.readouterr().err


def test_script(tmp_path: Path, document: dict[str, Any], capsys: pytest.CaptureFixture) -> None:
    path = tmp_path / "export.json"
    path.write_text(json.dumps(document))
    main = runpy.run_path(str(SCRIPT))["main"]
    assert main([str(path)]) == 0
    assert "Every digest and every result matches." in capsys.readouterr().out


def test_recount_uses_the_engine_only() -> None:
    source = Path(recount.__file__).read_text()
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert all("agaura" not in line or line == "from agaura import voting" for line in imports)


def test_worked_example_of_the_documentation() -> None:
    """docs/EXPORTS.md: the canonical line hashes to the digest shown, and recounts as stated."""
    doc = (Path(__file__).parents[2] / "docs" / "EXPORTS.md").read_text()
    line = next(line for line in doc.splitlines() if line.startswith('{"ballots":'))
    digest = re.search(r"^# ([0-9a-f]{64})$", doc, flags=re.MULTILINE).group(1)  # type: ignore[union-attr]
    document = json.loads(line)
    assert recount.canonical_json(document).decode() == line
    entry = {"definition": document["poll"], "ballots": document["ballots"]}
    assert recount.ballots_digest(entry) == digest
    result = recount.tally(entry, 3)
    assert result["counts"] == {"cast": 3, "blank": 1, "valid": 2}
    assert result["outcome"]["winner"] == 1 and result["outcome"]["adopted"] is True
