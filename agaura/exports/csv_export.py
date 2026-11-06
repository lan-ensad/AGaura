"""Anonymized ballots of one poll as CSV (FR-60).

Dialect, chosen so that French spreadsheets open the file with a double click: semicolon
separator (Excel in French expects it, since the comma is the decimal separator), UTF-8 with a
byte order mark (without it, Excel reads accented letters as Latin-1), CRLF line ends, double
quotes only around cells that need them. LibreOffice detects all of this by itself.

One row per ballot, in a random order drawn from the operating system's generator, so the
order reveals nothing about arrival times; no ballot id, no credential, no timestamp. Columns:
one per option (header: its label), then one for blank ballots (header: the blank label).

- single choice: ``1`` under the chosen option, ``0`` elsewhere;
- approval: ``1`` under each approved option, ``0`` elsewhere;
- Majority Judgment: the grade given to each option, as its translated label;
- Condorcet: the rank given to each option, ``1`` for the first choice, as normalized when the
  ballot was cast (dense ranks, the same for options put level, the options left unranked at
  the last rank);
- blank column: ``1`` for a blank ballot (its option cells are ``0`` or empty), else ``0``.

Text cells starting with a character that spreadsheets read as a formula get a leading
apostrophe (CSV injection), so a label such as ``=HYPERLINK(…)`` stays plain text.
"""

from __future__ import annotations

import csv
import io
import random
import secrets
from collections.abc import Mapping
from typing import Any

from agaura.models import Ballot, Poll
from agaura.services.exports import PollRecord
from agaura.voting.majority_judgment import DEFAULT_GRADE_COUNT
from agaura.web.labels import blank_label, grade_label

# Characters that make a spreadsheet evaluate a cell (OWASP), with their full-width forms.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "\uff1d", "\uff0b", "\uff0d", "\uff20")


class FrenchSpreadsheet(csv.Dialect):
    delimiter = ";"
    quotechar = '"'
    doublequote = True
    lineterminator = "\r\n"
    quoting = csv.QUOTE_MINIMAL
    skipinitialspace = False
    strict = True


def safe_cell(text: str) -> str:
    return f"'{text}" if text.startswith(FORMULA_PREFIXES) else text


def _cells(poll: Poll, content: Mapping[str, Any] | None) -> list[str]:
    positions = [option.position for option in poll.options]
    if poll.method == "condorcet":
        if content is None:
            return ["" for _ in positions]
        return [str(content["ranks"][str(position)]) for position in positions]
    if poll.method == "majority_judgment":
        if content is None:
            return ["" for _ in positions]
        grade_count = poll.params.get("grade_count", DEFAULT_GRADE_COUNT)
        grades = content["grades"]
        return [
            safe_cell(grade_label(grades[str(position)], grade_count)) for position in positions
        ]
    if content is None:
        chosen: set[int] = set()
    elif poll.method == "approval":
        chosen = set(content["approved"])
    else:
        chosen = {content["choice"]}
    return ["1" if position in chosen else "0" for position in positions]


def _row(poll: Poll, ballot: Ballot) -> list[str]:
    content = None if ballot.is_blank else ballot.content
    return [*_cells(poll, content), "1" if ballot.is_blank else "0"]


def ballots_csv(record: PollRecord, *, rng: random.Random | None = None) -> bytes:
    poll = record.poll
    rows = [_row(poll, ballot) for ballot in record.ballots or ()]
    (rng or secrets.SystemRandom()).shuffle(rows)
    header = [safe_cell(option.label) for option in poll.options] + [safe_cell(blank_label(poll))]
    buffer = io.StringIO()
    writer = csv.writer(buffer, dialect=FrenchSpreadsheet)
    writer.writerow(header)
    writer.writerows(rows)
    return ("\ufeff" + buffer.getvalue()).encode()
