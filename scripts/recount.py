"""Recount an AGaura JSON export independently (FR-63).

    uv run python scripts/recount.py agaura-K7M4QX-session.json
    uv run python scripts/recount.py agaura-K7M4QX-poll-3.json

For every poll of the file, recompute the SHA-256 digest of its definition and ballots, tally
the ballots again with ``agaura.voting`` alone (no database, no application code), and compare
both with what the server stored at closing. Exit status: 0 when everything matches, 1 on any
mismatch, 2 when the file cannot be read. The format and the procedure are described in
``docs/EXPORTS.md``.
"""

from __future__ import annotations

import sys

from agaura.exports.recount import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
