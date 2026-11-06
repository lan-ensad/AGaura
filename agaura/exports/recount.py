"""Independent recount of a JSON export (FR-63): every digest and result, from the file alone.

Only the standard library and the pure tallying engine (``agaura.voting``) are used: no
database, no application code. The canonical form is rebuilt here from its documentation
(``docs/EXPORTS.md``) rather than imported, so the check does not trust the code that produced
the file. Command line::

    uv run python scripts/recount.py agaura-K7M4QX-session.json

Exit status: 0 when every poll matches, 1 when a digest or a result differs, 2 when the file
cannot be read as an AGaura export.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from agaura import voting

FORMAT = "agaura-export"
VERSIONS = (1,)


class ExportFormatError(ValueError):
    """The file is not an AGaura export this script understands."""


def canonical_json(value: Any) -> bytes:
    """UTF-8 JSON, keys sorted, no insignificant whitespace, non-ASCII characters as is."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def ballots_digest(entry: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON of ``{"poll": definition, "ballots": ballots}``."""
    document = {"poll": entry["definition"], "ballots": entry["ballots"]}
    return hashlib.sha256(canonical_json(document)).hexdigest()


def tally(entry: Mapping[str, Any], attendees: int | None) -> dict[str, Any]:
    """The result as stored at closing: ``{"attendees": …, **outcome}``, from the file only."""
    definition = entry["definition"]
    spec = voting.PollSpec(
        method=definition["method"],
        options=tuple(option["id"] for option in definition["options"]),
        params=definition["params"],
    )
    ballots = [voting.Ballot(ballot["content"]) for ballot in entry["ballots"]]
    outcome = voting.tally(spec, ballots, attendees=attendees)
    return {"attendees": attendees, **voting.to_jsonable(outcome)}


def _plain(value: Any) -> Any:
    """Same value as it reads back from JSON (tuples become lists), for comparisons."""
    return json.loads(json.dumps(value))


@dataclass
class PollCheck:
    number: Any
    title: str
    status: str
    ballots: int
    digest: str
    stored_digest: str | None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def check_poll(entry: Mapping[str, Any]) -> PollCheck:
    ballots = entry["ballots"]
    result = entry.get("result")
    check = PollCheck(
        number=entry.get("number"),
        title=str(entry["definition"]["title"]),
        status=str(entry.get("status")),
        ballots=len(ballots),
        digest=ballots_digest(entry),
        stored_digest=None if result is None else result["ballots_sha256"],
    )
    ids = [ballot["id"] for ballot in ballots]
    if ids != sorted(ids):
        check.problems.append("ballots are not sorted by id")
    if len(set(ids)) != len(ids):
        check.problems.append("two ballots have the same id")
    if any(bool(ballot["is_blank"]) != (ballot["content"] is None) for ballot in ballots):
        check.problems.append("a ballot is marked blank but has a content, or the reverse")
    if entry.get("turnout") is not None and entry["turnout"] != len(ballots):
        check.problems.append(
            f"{entry['turnout']} participations but {len(ballots)} ballots in the file"
        )
    if result is None:
        # A poll cancelled while open has ballots but was never tallied.
        if check.status != "cancelled":
            check.problems.append("no stored result")
        return check
    if check.digest != check.stored_digest:
        check.problems.append("the digest of the ballots differs from the stored digest")
    stored = result["data"]
    try:
        recomputed = tally(entry, stored.get("attendees"))
    except voting.VotingError as error:
        check.problems.append(f"the ballots cannot be tallied: {error}")
    else:
        if _plain(recomputed) != _plain(stored):
            check.problems.append("the recomputed result differs from the stored result")
    return check


def check_document(document: Any) -> list[PollCheck]:
    if not isinstance(document, Mapping) or document.get("format") != FORMAT:
        raise ExportFormatError('not an AGaura export (missing "format": "agaura-export")')
    if document.get("version") not in VERSIONS:
        raise ExportFormatError(f"unsupported export version: {document.get('version')!r}")
    try:
        return [check_poll(entry) for entry in document["polls"]]
    except (KeyError, TypeError, AttributeError) as error:
        raise ExportFormatError(f"malformed export: {error!r}") from error


# Unicode categories written as escapes in the report: controls (ESC starts the terminal
# sequences that move the cursor, hide or recolour text), format characters (bidi overrides,
# zero-width), surrogates, private use, unassigned, line and paragraph separators.
HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def printable(text: str) -> str:
    """``text`` safe for a terminal: a forged export must not hide or rewrite the lines that
    say whether it matches (a title holding ESC[8m and a fake "OK" line, for instance)."""
    return "".join(
        f"\\u{ord(c):04x}" if unicodedata.category(c) in HIDDEN_CATEGORIES else c for c in text
    )


def report(document: Mapping[str, Any], checks: Sequence[PollCheck], out: TextIO) -> None:
    def say(text: str) -> None:
        print(printable(text), file=out)

    session = document.get("session") or {}
    say(f'Session {session.get("id")} "{session.get("title")}": {len(checks)} poll(s)')
    for check in checks:
        state = "OK" if check.ok else "MISMATCH"
        say(
            f'  #{check.number} "{check.title}" ({check.status}, {check.ballots} ballot(s)): '
            f"{state}"
        )
        say(f"      sha256 recomputed {check.digest}")
        if check.stored_digest is not None:
            say(f"      sha256 stored     {check.stored_digest}")
        else:
            say("      no stored result (poll cancelled before closing)")
        for problem in check.problems:
            say(f"      - {problem}")
    failed = sum(not check.ok for check in checks)
    if failed:
        say(f"{failed} poll(s) do not match.")
    else:
        say("Every digest and every result matches.")


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="recount", description="Recount every poll of an AGaura JSON export."
    )
    parser.add_argument("export", type=Path, help="JSON export of a poll or a session")
    args = parser.parse_args(argv)
    out = out or sys.stdout
    try:
        document = json.loads(args.export.read_text(encoding="utf-8"))
        checks = check_document(document)
    except (OSError, ValueError) as error:  # ExportFormatError and JSONDecodeError included
        print(printable(f"{args.export}: {error}"), file=sys.stderr)
        return 2
    report(document, checks, out)
    return 0 if all(check.ok for check in checks) else 1
