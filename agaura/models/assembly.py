"""Assembly sessions and voter access: code batches and voter credentials.

Ballot secrecy (SPEC §9.2): ``voter_credential`` carries no timestamp, and nothing links a
credential to a person.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint, false, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agaura.models.base import (
    AccessMode,
    AssemblyStatus,
    Base,
    BatchStatus,
    CredentialKind,
    str_enum,
    utcnow,
)

if TYPE_CHECKING:
    from agaura.models.account import User
    from agaura.models.poll import Poll


class Assembly(Base):
    __tablename__ = "assembly"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    public_id: Mapped[str] = mapped_column(String(6), unique=True)
    # A session of a demo account (FR-09a), set at creation: the voter pages, the screen and the
    # exports say so without reading the account.
    is_demo: Mapped[bool] = mapped_column(default=False, server_default=false())
    title: Mapped[str] = mapped_column(String(200))
    date: Mapped[dt.date | None]
    place: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[AssemblyStatus] = mapped_column(
        str_enum(AssemblyStatus), default=AssemblyStatus.PREPARATION
    )
    access_mode: Mapped[AccessMode] = mapped_column(
        str_enum(AccessMode), default=AccessMode.PASSWORD
    )
    # Encrypted rather than hashed: the tribune must be able to display them again (FR-20, FR-16).
    access_password_enc: Mapped[str] = mapped_column(String(500))
    screen_token_enc: Mapped[str] = mapped_column(String(500))
    public_token_enc: Mapped[str | None] = mapped_column(String(500))
    entries_locked: Mapped[bool] = mapped_column(default=False)
    registered_count: Mapped[int | None]
    quorum_rule: Mapped[str | None] = mapped_column(String(20))
    headcount: Mapped[int | None]
    opened_at: Mapped[dt.datetime | None]
    closed_at: Mapped[dt.datetime | None]
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    # The tribune: the people running the session, with functions typed freely, in display
    # order: ``[{"name": "Marie Dupont", "role": "Présidente de séance"}, …]`` (role may be "").
    tribune: Mapped[list[dict[str, str]]] = mapped_column(
        JSON, default=list, server_default=text("'[]'")
    )
    # Agenda, in order: ``[{"title": "Rapport moral"}, …]``. ``agenda_current`` is the position
    # (1, 2, …) of the item under discussion, highlighted on the projection screen; None: none.
    agenda: Mapped[list[dict[str, str]]] = mapped_column(
        JSON, default=list, server_default=text("'[]'")
    )
    agenda_current: Mapped[int | None]
    # Chronometer of the projection screen, run from the console. It counts down from
    # ``timer_duration`` seconds, or up when that is None. ``timer_elapsed`` holds the seconds
    # counted before the current run, which started at ``timer_started_at`` (None while paused).
    timer_shown: Mapped[bool] = mapped_column(default=False, server_default=false())
    timer_label: Mapped[str | None] = mapped_column(String(100))
    timer_duration: Mapped[int | None]
    timer_elapsed: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    timer_started_at: Mapped[dt.datetime | None]
    # Blocks of the projection screen the tribune hides (FR-70, from "Ce que la salle voit"):
    # the agenda, and the way to join (address, QR code, password) while entries are open.
    screen_agenda_hidden: Mapped[bool] = mapped_column(default=False, server_default=false())
    screen_join_hidden: Mapped[bool] = mapped_column(default=False, server_default=false())

    owner: Mapped[User] = relationship(back_populates="assemblies")
    polls: Mapped[list[Poll]] = relationship(
        back_populates="assembly", cascade="all, delete-orphan", passive_deletes=True
    )


class CodeBatch(Base):
    __tablename__ = "code_batch"

    id: Mapped[int] = mapped_column(primary_key=True)
    assembly_id: Mapped[int] = mapped_column(
        ForeignKey("assembly.id", ondelete="CASCADE"), index=True
    )
    size: Mapped[int]
    status: Mapped[BatchStatus] = mapped_column(str_enum(BatchStatus), default=BatchStatus.ACTIVE)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)


class VoterCredential(Base):
    """What entitles one ballot per poll. **No timestamp, ever.**

    Password mode: ``kind=device``, created when someone joins, ``token_hash`` is the digest of
    the device cookie. Codes mode: ``kind=code``, created with its batch, ``code_hmac`` is the
    digest of the printed code, and ``token_hash`` the digest of the cookie of the device that
    last entered the code. Leaving the session clears ``token_hash``.
    """

    __tablename__ = "voter_credential"
    __table_args__ = (UniqueConstraint("assembly_id", "code_hmac"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    assembly_id: Mapped[int] = mapped_column(
        ForeignKey("assembly.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[CredentialKind] = mapped_column(str_enum(CredentialKind))
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    code_hmac: Mapped[str | None] = mapped_column(String(64))
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("code_batch.id", ondelete="CASCADE"), index=True
    )
    activated: Mapped[bool] = mapped_column(default=False)

    batch: Mapped[CodeBatch | None] = relationship()
