"""Casting a ballot (FR-44, FR-45).

The participation record (*who* voted) and the ballot (*what* was voted) are written in the
same write transaction, and nothing links them. ``cast_vote`` must run inside
``Database.write()``: it relies on the transaction for atomicity, and on ``BEGIN IMMEDIATE`` to
serialize its double-vote check with concurrent submissions and with poll closing.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agaura import voting
from agaura.models import Ballot, Participation, Poll, PollStatus, VoterCredential, utcnow
from agaura.services import access
from agaura.services.errors import (
    AccessDeniedError,
    AlreadyVotedError,
    InvalidInputError,
    InvalidStateError,
    NotFoundError,
)
from agaura.services.results import spec_of


def is_accepting_votes(poll: Poll, now: datetime) -> bool:
    return poll.status is PollStatus.OPEN and (poll.closes_at is None or now < poll.closes_at)


def has_voted(db: Session, credential: VoterCredential, poll_id: int) -> bool:
    return (
        db.scalar(
            select(Participation.poll_id).where(
                Participation.poll_id == poll_id,
                Participation.credential_id == credential.id,
            )
        )
        is not None
    )


def cast_vote(
    db: Session,
    public_id: str,
    token: str | None,
    poll_id: int,
    content: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> None:
    """Record a ballot; ``content=None`` is a blank ballot.

    Raises ``AccessDeniedError`` (no valid credential), ``NotFoundError``,
    ``InvalidStateError`` (poll not open), ``InvalidInputError`` (invalid ballot, with the
    engine's error code) or ``AlreadyVotedError``.
    """
    now = now or utcnow()
    credential = access.credential_for(db, public_id, token)
    if credential is None:
        raise AccessDeniedError("invalid_credential")
    poll = db.get(Poll, poll_id)
    if poll is None or poll.assembly_id != credential.assembly_id:
        raise NotFoundError("poll")
    if not is_accepting_votes(poll, now):
        raise InvalidStateError("poll_not_open")
    if content is None:
        normalized = None
    else:
        try:
            normalized = voting.validate_ballot(spec_of(poll), content)
        except voting.InvalidBallotError as error:
            raise InvalidInputError(error.code, error.detail) from error

    if has_voted(db, credential, poll.id):
        raise AlreadyVotedError
    db.add(Participation(poll_id=poll.id, credential_id=credential.id))
    db.add(
        Ballot(id=uuid.uuid4().hex, poll_id=poll.id, is_blank=content is None, content=normalized)
    )
    try:
        db.flush()
    except IntegrityError as error:  # backstop: primary key (poll, credential)
        raise AlreadyVotedError from error
