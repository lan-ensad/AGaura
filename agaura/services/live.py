"""Read-only queries behind the voter live page (FR-41).

The live page shows only the polls currently open, then the results the tribune has published
to voters, each group most recent first: the latest poll sits on top of the pile. Writes
(joining, voting, leaving) go through ``access`` and ``ballots``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from agaura.models import Participation, Poll, PollStatus, VoterCredential, utcnow
from agaura.services.ballots import is_accepting_votes

_EPOCH = dt.datetime.min.replace(tzinfo=dt.UTC)


def _pushed_order(poll: Poll) -> tuple[dt.datetime, int]:
    return (poll.opened_at or _EPOCH, poll.id)


def open_polls(db: Session, assembly_id: int, *, now: dt.datetime | None = None) -> list[Poll]:
    """Polls accepting votes, with their options, most recently pushed first."""
    now = now or utcnow()
    polls = db.scalars(
        select(Poll)
        .options(selectinload(Poll.options))
        .where(Poll.assembly_id == assembly_id, Poll.status == PollStatus.OPEN)
    ).all()
    # A poll whose timer ran out stops accepting votes before the periodic job closes it.
    accepting = (poll for poll in polls if is_accepting_votes(poll, now))
    return sorted(accepting, key=_pushed_order, reverse=True)


def results_for_voters(db: Session, assembly_id: int) -> list[Poll]:
    """Published results shown on the voter page and not dismissed, most recent poll first."""
    polls = db.scalars(
        select(Poll)
        .options(selectinload(Poll.options), selectinload(Poll.result))
        .where(
            Poll.assembly_id == assembly_id,
            Poll.status == PollStatus.PUBLISHED,
            Poll.shown_to_voters.is_(True),
        )
    ).all()
    published = (poll for poll in polls if poll.result is not None)
    return sorted(published, key=_pushed_order, reverse=True)


def voted_poll_ids(db: Session, credential: VoterCredential, poll_ids: Iterable[int]) -> set[int]:
    """Among ``poll_ids``, the polls this credential has already voted on."""
    ids = list(poll_ids)
    if not ids:
        return set()
    return set(
        db.scalars(
            select(Participation.poll_id).where(
                Participation.credential_id == credential.id, Participation.poll_id.in_(ids)
            )
        )
    )


def voter_poll(db: Session, assembly_id: int, poll_id: int) -> Poll | None:
    """A poll of the session that voters may know about (any state but draft), with options."""
    return db.scalar(
        select(Poll)
        .options(selectinload(Poll.options))
        .where(
            Poll.id == poll_id,
            Poll.assembly_id == assembly_id,
            Poll.status != PollStatus.DRAFT,
        )
    )


@dataclass(frozen=True, slots=True)
class LiveView:
    """Everything one voter's live page shows."""

    open_polls: list[Poll]
    voted: frozenset[int]  # ids of the open polls this credential has voted on
    results: list[Poll]


def live_view(
    db: Session, credential: VoterCredential, *, now: dt.datetime | None = None
) -> LiveView:
    polls = open_polls(db, credential.assembly_id, now=now)
    return LiveView(
        open_polls=polls,
        voted=frozenset(voted_poll_ids(db, credential, (poll.id for poll in polls))),
        results=results_for_voters(db, credential.assembly_id),
    )
