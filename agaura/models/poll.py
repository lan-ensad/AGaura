"""Polls, their options, participations, ballots and stored results.

Ballot secrecy (SPEC §9.2):

- ``participation`` records *that* a credential voted on a poll, ``ballot`` records *what* was
  voted; no column links the two, and neither carries a timestamp;
- both tables are ``WITHOUT ROWID``, and a ballot's primary key is a random UUID, so rows are
  read back in random-id order;
- the file pages still keep the arrival order while a poll is open, until closing the poll
  rebuilds the file in key order (``agaura.db.compact_after_commit``).

Options are identified in ballots and results by their ``position`` (1, 2, 3…), which never
changes once the poll leaves the draft state.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agaura.models.base import Base, PollStatus, str_enum, utcnow

if TYPE_CHECKING:
    from agaura.models.assembly import Assembly


class Poll(Base):
    __tablename__ = "poll"

    id: Mapped[int] = mapped_column(primary_key=True)
    assembly_id: Mapped[int] = mapped_column(
        ForeignKey("assembly.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int]
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str] = mapped_column(String(40))
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # None: the interface shows its default label ("Vote blanc").
    blank_label: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[PollStatus] = mapped_column(str_enum(PollStatus), default=PollStatus.DRAFT)
    shown_to_voters: Mapped[bool] = mapped_column(default=False)
    # The tribune shows the result on the projection screen (FR-75), apart from publishing it
    # to voters; ``screen_shown_at`` is when, so the latest result shown takes the stage. None
    # while not shown. A time on the poll says nothing about any ballot or voter.
    shown_on_screen: Mapped[bool] = mapped_column(default=False)
    screen_shown_at: Mapped[datetime | None]
    # A trial vote of the room's network ("Tout le monde est-il connecté ?", D-I): run like any
    # poll, but left out of the minutes, the session export and the public results (FR-61,
    # FR-62). A flag of the poll only: nothing on its ballots or participations.
    trial: Mapped[bool] = mapped_column(default=False, server_default=false())
    opened_at: Mapped[datetime | None]
    closes_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    cancel_reason: Mapped[str | None] = mapped_column(Text)

    assembly: Mapped[Assembly] = relationship(back_populates="polls")
    options: Mapped[list[Option]] = relationship(
        back_populates="poll",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Option.position",
    )
    result: Mapped[Result | None] = relationship(cascade="all, delete-orphan", passive_deletes=True)


class Option(Base):
    __tablename__ = "option"
    __table_args__ = (UniqueConstraint("poll_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    poll_id: Mapped[int] = mapped_column(ForeignKey("poll.id", ondelete="CASCADE"), index=True)
    position: Mapped[int]
    label: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)

    poll: Mapped[Poll] = relationship(back_populates="options")


class Participation(Base):
    """A credential has voted on a poll. **No timestamp, no link to the ballot.**"""

    __tablename__ = "participation"
    __table_args__ = {"sqlite_with_rowid": False}  # noqa: RUF012

    poll_id: Mapped[int] = mapped_column(
        ForeignKey("poll.id", ondelete="CASCADE"), primary_key=True
    )
    credential_id: Mapped[int] = mapped_column(
        ForeignKey("voter_credential.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class Ballot(Base):
    """What was voted. **No timestamp, no link to the credential.**"""

    __tablename__ = "ballot"
    __table_args__ = {"sqlite_with_rowid": False}  # noqa: RUF012

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # random UUID, hex
    poll_id: Mapped[int] = mapped_column(ForeignKey("poll.id", ondelete="CASCADE"), index=True)
    is_blank: Mapped[bool]
    content: Mapped[dict[str, Any] | None]


class Result(Base):
    __tablename__ = "result"

    poll_id: Mapped[int] = mapped_column(
        ForeignKey("poll.id", ondelete="CASCADE"), primary_key=True
    )
    data: Mapped[dict[str, Any]]
    ballots_sha256: Mapped[str] = mapped_column(String(64))
    computed_at: Mapped[datetime] = mapped_column(default=utcnow)
    published_at: Mapped[datetime | None]
