"""Read models for the dashboard and the session console (owner-scoped, read-only)."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agaura.models import (
    Assembly,
    AuditEntry,
    CodeBatch,
    Participation,
    Poll,
    PollStatus,
    User,
)
from agaura.services import access, assemblies, polls

LOG_LIMIT = 100


@dataclass(frozen=True, slots=True)
class AssemblySummary:
    assembly: Assembly
    polls: int
    attendees: int


def summaries(db: Session, owner: User) -> list[AssemblySummary]:
    """The dashboard list (FR-10): most recent first, with counts. The trial vote of the
    checklist (D-I) is not a poll of the session: it counts nowhere, as in the minutes."""
    rows = assemblies.list_assemblies(db, owner)
    poll_counts = dict(
        db.execute(
            select(Poll.assembly_id, func.count(Poll.id))
            .join(Assembly, Poll.assembly_id == Assembly.id)
            .where(Assembly.owner_id == owner.id, Poll.trial.is_(False))
            .group_by(Poll.assembly_id)
        ).all()
    )
    return [
        AssemblySummary(
            assembly=assembly,
            polls=poll_counts.get(assembly.id, 0),
            attendees=access.attendees(db, assembly),
        )
        for assembly in rows
    ]


@dataclass(frozen=True, slots=True)
class PollView:
    poll: Poll
    turnout: int


@dataclass
class LivePanel:
    assembly: Assembly
    attendees: int
    quorum: int | None
    warnings: list[access.HeadcountWarning]
    open_polls: list[PollView]
    access_cap: int | None = None  # the cap of password accesses, once reached (FR-27)


@dataclass
class ConsoleState:
    panel: LivePanel
    drafts: list[Poll]
    finished: list[PollView]  # closed, published or cancelled: most recent first
    batches: list[CodeBatch] = field(default_factory=list)
    log: list[AuditEntry] = field(default_factory=list)

    @property
    def assembly(self) -> Assembly:
        return self.panel.assembly

    @property
    def just_closed(self) -> PollView | None:
        """The poll closed last, while its result is neither published to voters nor shown on
        the screen: the "Vote clos" command heads Steering until the tribune does either (D-K).
        None once that poll is published, shown or cancelled."""
        ended = [view for view in self.finished if view.poll.closed_at is not None]
        if not ended:
            return None
        last = max(ended, key=lambda view: (view.poll.closed_at, view.poll.id))
        poll = last.poll
        if poll.status is not PollStatus.CLOSED or poll.shown_on_screen or poll.result is None:
            return None
        return last


def _turnouts(db: Session, assembly: Assembly) -> dict[int, int]:
    return dict(
        db.execute(
            select(Participation.poll_id, func.count(Participation.credential_id))
            .join(Poll, Participation.poll_id == Poll.id)
            .where(Poll.assembly_id == assembly.id)
            .group_by(Participation.poll_id)
        ).all()
    )


def live_panel(db: Session, owner: User, public_id: str) -> LivePanel:
    assembly = assemblies.get_assembly(db, owner, public_id)
    turnouts = _turnouts(db, assembly)
    open_polls = db.scalars(
        select(Poll)
        .where(Poll.assembly_id == assembly.id, Poll.status == PollStatus.OPEN)
        .order_by(Poll.opened_at, Poll.id)
    ).all()
    present = access.attendees(db, assembly)
    return LivePanel(
        assembly=assembly,
        attendees=present,
        quorum=assemblies.quorum_required(assembly),
        warnings=access.headcount_warnings(db, assembly),
        open_polls=[PollView(poll, turnouts.get(poll.id, 0)) for poll in open_polls],
        access_cap=access.access_cap_reached(assembly, present),
    )


def drafts(db: Session, owner: User, public_id: str) -> list[Poll]:
    """The drafts of a session in their order, as ``ConsoleState.drafts`` lists them: the
    "Prochain vote" command of the live panel, in one query."""
    return list(
        db.scalars(
            select(Poll)
            .join(Assembly, Poll.assembly_id == Assembly.id)
            .where(
                Assembly.owner_id == owner.id,
                Assembly.public_id == assemblies.normalize_public_id(public_id),
                Poll.status == PollStatus.DRAFT,
            )
            .order_by(Poll.position, Poll.id)
        ).all()
    )


def console_state(db: Session, owner: User, public_id: str) -> ConsoleState:
    panel = live_panel(db, owner, public_id)
    assembly = panel.assembly
    turnouts = _turnouts(db, assembly)
    every = polls.list_polls(db, owner, assembly.public_id)
    finished = [
        PollView(poll, turnouts.get(poll.id, 0))
        for poll in every
        if poll.status in (PollStatus.CLOSED, PollStatus.PUBLISHED, PollStatus.CANCELLED)
    ]
    finished.reverse()
    log = db.scalars(
        select(AuditEntry)
        .where(AuditEntry.assembly_id == assembly.id)
        .order_by(AuditEntry.id.desc())
        .limit(LOG_LIMIT)
    ).all()
    return ConsoleState(
        panel=panel,
        drafts=[poll for poll in every if poll.status is PollStatus.DRAFT],
        finished=finished,
        batches=access.list_code_batches(db, owner, assembly.public_id),
        log=list(log),
    )
