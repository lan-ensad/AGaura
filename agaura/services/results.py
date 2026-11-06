"""Tallying a closed poll, and the canonical form behind its SHA-256 digest (FR-50, FR-63).

Canonical form, also used by the JSON export so anyone can recount::

    {
      "poll": {"title", "description", "method", "params", "blank_label",
               "options": [{"id": <position>, "label", "description"}, ...]},
      "ballots": [{"id", "is_blank", "content"}, ...]      # sorted by id
    }

serialized as UTF-8 JSON with sorted keys and no insignificant whitespace.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agaura import voting
from agaura.models import Ballot, Participation, Poll, Result, utcnow
from agaura.services import access


def spec_of(poll: Poll) -> voting.PollSpec:
    """Options are identified by their position in the poll (1, 2, 3…)."""
    return voting.PollSpec(
        method=poll.method,
        options=tuple(option.position for option in poll.options),
        params=poll.params,
    )


def canonical_poll(poll: Poll) -> dict[str, Any]:
    return {
        "title": poll.title,
        "description": poll.description,
        "method": poll.method,
        "params": poll.params,
        "blank_label": poll.blank_label,
        "options": [
            {"id": option.position, "label": option.label, "description": option.description}
            for option in poll.options
        ],
    }


def canonical_ballots(ballots: Iterable[Ballot]) -> list[dict[str, Any]]:
    return [
        {"id": ballot.id, "is_blank": ballot.is_blank, "content": ballot.content}
        for ballot in sorted(ballots, key=lambda ballot: ballot.id)
    ]


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def ballots_sha256(poll: Poll, ballots: Iterable[Ballot]) -> str:
    document = {"poll": canonical_poll(poll), "ballots": canonical_ballots(ballots)}
    return hashlib.sha256(canonical_json(document)).hexdigest()


def turnout(db: Session, poll_id: int) -> int:
    count = db.scalar(
        select(func.count()).select_from(Participation).where(Participation.poll_id == poll_id)
    )
    return count or 0


def compute_result(db: Session, poll: Poll, *, now: datetime | None = None) -> Result:
    """Tally every ballot of the poll and store the result with its digest."""
    ballots = list(db.scalars(select(Ballot).where(Ballot.poll_id == poll.id)))
    present = access.attendees(db, poll.assembly)
    outcome = voting.tally(
        spec_of(poll), [voting.Ballot(ballot.content) for ballot in ballots], attendees=present
    )
    result = Result(
        poll_id=poll.id,
        data={"attendees": present, **voting.to_jsonable(outcome)},
        ballots_sha256=ballots_sha256(poll, ballots),
        computed_at=now or utcnow(),
    )
    poll.result = result
    db.flush()
    return result
