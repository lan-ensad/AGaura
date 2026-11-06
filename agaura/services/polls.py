"""Polls: drafts, lifecycle (SPEC §7), timers, publication of results (FR-15, FR-30 to FR-36),
results on the projection screen (FR-75).

Lifecycle: draft → open → closed → published, and open / closed / published → cancelled.
Only drafts can be edited, reordered or deleted. Cancelling a published result also withdraws it,
from the voter page and from the projection screen. Publishing to voters and showing on the
screen are two separate choices of the tribune.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agaura import voting
from agaura.db import compact_after_commit
from agaura.models import Assembly, AssemblyStatus, Option, Poll, PollStatus, Result, User, utcnow
from agaura.services import attendance, audit, meeting, results
from agaura.services.accounts import quotas
from agaura.services.assemblies import clean_text, get_assembly, require_status
from agaura.services.audit import Actor
from agaura.services.errors import (
    InvalidInputError,
    InvalidStateError,
    NotFoundError,
    QuotaExceededError,
)

MAX_POLL_TITLE_LENGTH = 200
MAX_POLL_DESCRIPTION_LENGTH = 5000
MAX_OPTION_LABEL_LENGTH = 200
MAX_OPTION_DESCRIPTION_LENGTH = 1000
MAX_BLANK_LABEL_LENGTH = 100
MAX_CANCEL_REASON_LENGTH = 500
MAX_TIMER_MINUTES = 24 * 60

_ACTIVE_STATUSES = (AssemblyStatus.PREPARATION, AssemblyStatus.IN_SESSION)


@dataclass(frozen=True, slots=True)
class OptionInput:
    label: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class PollInput:
    """A poll definition. Parameters refer to options by position: ``adopt_option: 1``."""

    title: str
    method: str
    options: Sequence[OptionInput | str]
    params: Mapping[str, Any] = field(default_factory=dict)
    description: str | None = None
    blank_label: str | None = None


@dataclass(frozen=True, slots=True)
class _CleanPoll:
    title: str
    description: str | None
    method: str
    params: dict[str, Any]
    blank_label: str | None
    options: list[OptionInput]


def _option_input(raw: OptionInput | str) -> OptionInput:
    return OptionInput(raw) if isinstance(raw, str) else raw


def _spec_field(error: voting.InvalidSpecError) -> str:
    """Form field concerned by an engine error: ``threshold.fraction`` → ``threshold``."""
    if error.code == "option_count":
        return "options"
    if error.code == "unknown_method":
        return "method"
    if error.code == "invalid_param" and error.detail:
        return error.detail.split(".")[0]
    return "params"


def poll_problems(data: PollInput) -> list[InvalidInputError]:
    """Every problem of a poll definition, so that a form can show them all at once.

    Each error's ``detail`` names the field: ``title``, ``description``, ``blank_label``,
    ``option:<n>`` and ``option_description:<n>`` (``n`` counts from 1 in ``data.options``),
    ``options`` (count, duplicates), ``method``, or a parameter (``seats``, ``threshold``…).
    """
    problems: list[InvalidInputError] = []

    def check(value: str | None, field: str, *, max_length: int, required: bool) -> None:
        try:
            clean_text(value, max_length=max_length, field=field, required=required)
        except InvalidInputError as error:
            problems.append(InvalidInputError(error.code, field))

    check(data.title, "title", max_length=MAX_POLL_TITLE_LENGTH, required=True)
    check(data.description, "description", max_length=MAX_POLL_DESCRIPTION_LENGTH, required=False)
    labels: list[str] = []
    for number, raw in enumerate(data.options, start=1):
        option = _option_input(raw)
        check(option.label, f"option:{number}", max_length=MAX_OPTION_LABEL_LENGTH, required=True)
        check(
            option.description,
            f"option_description:{number}",
            max_length=MAX_OPTION_DESCRIPTION_LENGTH,
            required=False,
        )
        if label := (option.label or "").strip().casefold():
            labels.append(label)
    if len(set(labels)) != len(labels):
        problems.append(InvalidInputError("duplicate_option_label", "options"))
    check(data.blank_label, "blank_label", max_length=MAX_BLANK_LABEL_LENGTH, required=False)
    spec = voting.PollSpec(data.method, tuple(range(1, len(data.options) + 1)), dict(data.params))
    try:
        voting.check_spec(spec)
    except voting.InvalidSpecError as error:
        problems.append(InvalidInputError(error.code, _spec_field(error)))
    return problems


def _clean_option(raw: OptionInput | str) -> OptionInput:
    option = _option_input(raw)
    return OptionInput(
        (option.label or "").strip(),
        clean_text(
            option.description,
            max_length=MAX_OPTION_DESCRIPTION_LENGTH,
            field="option_description",
            required=False,
        ),
    )


def _validated(data: PollInput) -> _CleanPoll:
    if problems := poll_problems(data):
        raise problems[0]
    return _CleanPoll(
        title=(data.title or "").strip(),
        description=clean_text(
            data.description,
            max_length=MAX_POLL_DESCRIPTION_LENGTH,
            field="description",
            required=False,
        ),
        method=data.method,
        params=dict(data.params),
        blank_label=clean_text(
            data.blank_label,
            max_length=MAX_BLANK_LABEL_LENGTH,
            field="blank_label",
            required=False,
        ),
        options=[_clean_option(option) for option in data.options],
    )


def _apply(poll: Poll, data: _CleanPoll) -> None:
    poll.title = data.title
    poll.description = data.description
    poll.method = data.method
    poll.params = data.params
    poll.blank_label = data.blank_label
    # Update rows in place: positions stay unique at every step of the flush.
    existing = list(poll.options)
    for position, option in enumerate(data.options, start=1):
        if position <= len(existing):
            existing[position - 1].label = option.label
            existing[position - 1].description = option.description
        else:
            poll.options.append(
                Option(position=position, label=option.label, description=option.description)
            )
    for extra in existing[len(data.options) :]:
        poll.options.remove(extra)


def _check_poll_quota(db: Session, assembly: Assembly, adding: int = 1) -> None:
    count = db.scalar(select(func.count()).select_from(Poll).where(Poll.assembly_id == assembly.id))
    if (count or 0) + adding > quotas(assembly.owner).polls_per_assembly:
        raise QuotaExceededError("polls_per_assembly")


def _next_position(db: Session, assembly: Assembly) -> int:
    last = db.scalar(select(func.max(Poll.position)).where(Poll.assembly_id == assembly.id))
    return (last or 0) + 1


def _new_draft(db: Session, assembly: Assembly, data: _CleanPoll, *, trial: bool = False) -> Poll:
    poll = Poll(assembly_id=assembly.id, position=_next_position(db, assembly), trial=trial)
    _apply(poll, data)
    db.add(poll)
    db.flush()
    return poll


def get_poll(db: Session, owner: User, poll_id: int) -> Poll:
    poll = db.scalar(
        select(Poll)
        .join(Assembly, Poll.assembly_id == Assembly.id)
        .where(Poll.id == poll_id, Assembly.owner_id == owner.id)
    )
    if poll is None:
        raise NotFoundError("poll")
    return poll


def list_polls(db: Session, owner: User, public_id: str) -> list[Poll]:
    """Polls already pushed, in the order they were opened, then drafts in their order."""
    assembly = get_assembly(db, owner, public_id)
    polls = db.scalars(select(Poll).where(Poll.assembly_id == assembly.id)).all()
    pushed = sorted(
        (poll for poll in polls if poll.status is not PollStatus.DRAFT),
        key=lambda poll: (poll.opened_at or dt.datetime.min.replace(tzinfo=dt.UTC), poll.id),
    )
    drafts = sorted(
        (poll for poll in polls if poll.status is PollStatus.DRAFT),
        key=lambda poll: (poll.position, poll.id),
    )
    return [*pushed, *drafts]


def create_poll(
    db: Session,
    owner: User,
    public_id: str,
    data: PollInput,
    *,
    trial: bool = False,
    now: dt.datetime | None = None,
) -> Poll:
    """A new draft. ``trial``: a trial vote of the room's network (``Poll.trial``), left out of
    the minutes and the session export."""
    assembly = get_assembly(db, owner, public_id)
    require_status(assembly, *_ACTIVE_STATUSES)
    clean = _validated(data)
    _check_poll_quota(db, assembly)
    poll = _new_draft(db, assembly, clean, trial=trial)
    details: dict[str, Any] = {"poll_id": poll.id, "title": poll.title, "method": poll.method}
    if trial:
        details["trial"] = True
    audit.record(
        db, "poll.created", user_id=owner.id, assembly_id=assembly.id, details=details, now=now
    )
    return poll


def create_trial_poll(
    db: Session, owner: User, public_id: str, data: PollInput, *, now: dt.datetime | None = None
) -> Poll:
    """The trial vote of the checklist (D-I), once: refused while a trial draft is waiting. It
    comes first among the drafts, as the checklist says to run it first: "Prochain vote"
    offers it."""
    assembly = get_assembly(db, owner, public_id)
    waiting = db.scalar(
        select(Poll.id).where(
            Poll.assembly_id == assembly.id, Poll.trial.is_(True), Poll.status == PollStatus.DRAFT
        )
    )
    if waiting is not None:
        raise InvalidStateError("poll_trial_waiting")
    first = db.scalar(
        select(func.min(Poll.position)).where(
            Poll.assembly_id == assembly.id, Poll.status == PollStatus.DRAFT
        )
    )
    poll = create_poll(db, owner, public_id, data, trial=True, now=now)
    if first is not None:
        poll.position = first - 1
        db.flush()
    return poll


def _require_draft(poll: Poll) -> None:
    if poll.status is not PollStatus.DRAFT:
        raise InvalidStateError(f"poll_{poll.status.value}")


def update_poll(
    db: Session, owner: User, poll_id: int, data: PollInput, *, now: dt.datetime | None = None
) -> Poll:
    poll = get_poll(db, owner, poll_id)
    _require_draft(poll)
    require_status(poll.assembly, *_ACTIVE_STATUSES)
    _apply(poll, _validated(data))
    db.flush()
    audit.record(
        db,
        "poll.updated",
        user_id=owner.id,
        assembly_id=poll.assembly_id,
        details={"poll_id": poll.id},
        now=now,
    )
    return poll


def delete_poll(db: Session, owner: User, poll_id: int, *, now: dt.datetime | None = None) -> None:
    poll = get_poll(db, owner, poll_id)
    _require_draft(poll)
    audit.record(
        db,
        "poll.deleted",
        user_id=owner.id,
        assembly_id=poll.assembly_id,
        details={"poll_id": poll.id, "title": poll.title},
        now=now,
    )
    db.delete(poll)
    db.flush()


def _definition(poll: Poll) -> _CleanPoll:
    return _CleanPoll(
        title=poll.title,
        description=poll.description,
        method=poll.method,
        params=dict(poll.params),
        blank_label=poll.blank_label,
        options=[OptionInput(option.label, option.description) for option in poll.options],
    )


def duplicate_poll(
    db: Session, owner: User, poll_id: int, *, now: dt.datetime | None = None
) -> Poll:
    """Copy any poll as a new draft, e.g. to rerun a vote in codes mode (FR-28)."""
    source = get_poll(db, owner, poll_id)
    assembly = source.assembly
    require_status(assembly, *_ACTIVE_STATUSES)
    _check_poll_quota(db, assembly)
    poll = _new_draft(db, assembly, _definition(source), trial=source.trial)
    audit.record(
        db,
        "poll.duplicated",
        user_id=owner.id,
        assembly_id=assembly.id,
        details={"poll_id": poll.id, "source_poll_id": source.id},
        now=now,
    )
    return poll


def copy_polls(
    db: Session,
    owner: User,
    source_public_id: str,
    target_public_id: str,
    *,
    now: dt.datetime | None = None,
) -> list[Poll]:
    """Copy the polls of another session as drafts, except cancelled ones (FR-15).

    Both sessions must belong to ``owner``: another account's session raises ``NotFoundError``,
    exactly like a missing one. Copies come after the target's drafts, in the source's order.
    """
    source = get_assembly(db, owner, source_public_id)
    target = get_assembly(db, owner, target_public_id)
    require_status(target, *_ACTIVE_STATUSES)
    if source.id == target.id:
        raise InvalidInputError("editor_copy_same_session")
    originals = _copyable(db, owner, source)
    if not originals:
        raise InvalidInputError("editor_copy_empty")
    _check_poll_quota(db, target, len(originals))
    copies = [_new_draft(db, target, _definition(poll), trial=poll.trial) for poll in originals]
    audit.record(
        db,
        "polls.copied",
        user_id=owner.id,
        assembly_id=target.id,
        details={"source_public_id": source.public_id, "count": len(copies)},
        now=now,
    )
    return copies


def _copyable(db: Session, owner: User, source: Assembly) -> list[Poll]:
    return [
        poll
        for poll in list_polls(db, owner, source.public_id)
        if poll.status is not PollStatus.CANCELLED
    ]


@dataclass(frozen=True, slots=True)
class CopySource:
    assembly: Assembly
    polls: int  # polls that ``copy_polls`` would copy


def copy_sources(db: Session, owner: User, public_id: str) -> list[CopySource]:
    """The account's other sessions with polls to copy, most recent first (FR-15)."""
    target = get_assembly(db, owner, public_id)
    counts = dict(
        db.execute(
            select(Poll.assembly_id, func.count(Poll.id))
            .join(Assembly, Poll.assembly_id == Assembly.id)
            .where(
                Assembly.owner_id == owner.id,
                Assembly.id != target.id,
                Poll.status != PollStatus.CANCELLED,
            )
            .group_by(Poll.assembly_id)
        ).all()
    )
    sources = db.scalars(
        select(Assembly)
        .where(Assembly.id.in_(list(counts)))
        .order_by(Assembly.created_at.desc(), Assembly.id.desc())
    )
    return [CopySource(assembly, counts[assembly.id]) for assembly in sources]


def reorder_drafts(
    db: Session,
    owner: User,
    public_id: str,
    poll_ids: Sequence[int],
    *,
    now: dt.datetime | None = None,
) -> None:
    """Set the order of the drafts; ``poll_ids`` must list every draft exactly once."""
    assembly = get_assembly(db, owner, public_id)
    drafts = {
        poll.id: poll
        for poll in db.scalars(
            select(Poll).where(Poll.assembly_id == assembly.id, Poll.status == PollStatus.DRAFT)
        )
    }
    if len(poll_ids) != len(drafts) or set(poll_ids) != set(drafts):
        raise InvalidInputError("invalid_order")
    for position, poll_id in enumerate(poll_ids, start=1):
        drafts[poll_id].position = position
    audit.record(db, "polls.reordered", user_id=owner.id, assembly_id=assembly.id, now=now)


MOVES = {"up": -1, "down": 1}


def move_draft(
    db: Session, owner: User, poll_id: int, direction: str, *, now: dt.datetime | None = None
) -> bool:
    """Move a draft one place ``up`` or ``down`` among the drafts of its session (FR-32).

    Returns False, and changes nothing, when the draft is already first (up) or last (down).
    """
    if direction not in MOVES:
        raise InvalidInputError("invalid_order")
    poll = get_poll(db, owner, poll_id)
    _require_draft(poll)
    assembly = poll.assembly
    require_status(assembly, *_ACTIVE_STATUSES)
    drafts = list(
        db.scalars(
            select(Poll)
            .where(Poll.assembly_id == assembly.id, Poll.status == PollStatus.DRAFT)
            .order_by(Poll.position, Poll.id)
        )
    )
    index = drafts.index(poll)
    other = index + MOVES[direction]
    if not 0 <= other < len(drafts):
        return False
    drafts[index], drafts[other] = drafts[other], drafts[index]
    reorder_drafts(db, owner, assembly.public_id, [draft.id for draft in drafts], now=now)
    return True


def _agenda_item(assembly: Assembly) -> dict[str, int | str]:
    position, title = meeting.current_item(assembly)
    return {} if position is None else {"agenda_position": position, "agenda_title": title or ""}


def open_poll(
    db: Session,
    owner: User,
    poll_id: int,
    *,
    minutes: int | None = None,
    now: dt.datetime | None = None,
) -> Poll:
    """Push a draft to voters, with an optional auto-close timer (FR-33)."""
    now = now or utcnow()
    poll = get_poll(db, owner, poll_id)
    require_status(poll.assembly, AssemblyStatus.IN_SESSION)
    _require_draft(poll)
    if minutes is not None and not 1 <= minutes <= MAX_TIMER_MINUTES:
        raise InvalidInputError("invalid_timer")
    poll.status = PollStatus.OPEN
    poll.opened_at = now
    poll.closes_at = None if minutes is None else now + dt.timedelta(minutes=minutes)
    audit.record(
        db,
        "poll.opened",
        user_id=owner.id,
        assembly_id=poll.assembly_id,
        # With an attendance reading (services/attendance.py) and the agenda item under
        # discussion, which places the poll in the minutes (services/meeting.py).
        details={
            "poll_id": poll.id,
            "timer_minutes": minutes,
            **attendance.counts(db, poll.assembly),
            **_agenda_item(poll.assembly),
        },
        now=now,
    )
    return poll


def _close(db: Session, poll: Poll, *, actor: str, now: dt.datetime) -> Result:
    poll.status = PollStatus.CLOSED
    poll.closed_at = now
    result = results.compute_result(db, poll, now=now)
    compact_after_commit(db)  # erase the arrival order of ballots from the files (SPEC §9.2)
    audit.record(
        db,
        "poll.closed",
        actor=actor,
        user_id=poll.assembly.owner_id,
        assembly_id=poll.assembly_id,
        details={
            "poll_id": poll.id,
            "ballots": result.data["counts"]["cast"],
            "ballots_sha256": result.ballots_sha256,
            **attendance.counts(db, poll.assembly),  # attendance reading
        },
        now=now,
    )
    return result


def close_poll(db: Session, owner: User, poll_id: int, *, now: dt.datetime | None = None) -> Result:
    """Close an open poll: voting stops, the result and the digest are computed (FR-34)."""
    poll = get_poll(db, owner, poll_id)
    if poll.status is not PollStatus.OPEN:
        raise InvalidStateError(f"poll_{poll.status.value}")
    return _close(db, poll, actor=Actor.USER, now=now or utcnow())


def close_expired_polls(db: Session, *, now: dt.datetime | None = None) -> list[Poll]:
    """Close every open poll whose timer has run out, including after a restart (SPEC §10)."""
    now = now or utcnow()
    expired = db.scalars(
        select(Poll)
        .where(Poll.status == PollStatus.OPEN, Poll.closes_at.is_not(None), Poll.closes_at <= now)
        .order_by(Poll.closes_at, Poll.id)
    ).all()
    for poll in expired:
        _close(db, poll, actor=Actor.SYSTEM, now=now)
    return list(expired)


def cancel_poll(
    db: Session, owner: User, poll_id: int, reason: str, *, now: dt.datetime | None = None
) -> Poll:
    """Cancel with a mandatory reason; ballots are kept, no result is shown (FR-35)."""
    now = now or utcnow()
    poll = get_poll(db, owner, poll_id)
    if poll.status not in (PollStatus.OPEN, PollStatus.CLOSED, PollStatus.PUBLISHED):
        raise InvalidStateError(f"poll_{poll.status.value}")
    clean_reason = clean_text(
        reason, max_length=MAX_CANCEL_REASON_LENGTH, field="reason", required=True
    )
    if poll.status is PollStatus.OPEN:
        poll.closed_at = now
    poll.status = PollStatus.CANCELLED
    poll.cancel_reason = clean_reason
    compact_after_commit(db)  # its ballots are kept (SPEC §9.2)
    poll.shown_to_voters = poll.shown_on_screen = False  # never a cancelled result (FR-72)
    poll.screen_shown_at = None
    audit.record(
        db,
        "poll.cancelled",
        user_id=owner.id,
        assembly_id=poll.assembly_id,
        details={"poll_id": poll.id, "reason": clean_reason},
        now=now,
    )
    return poll


def publish_result(
    db: Session, owner: User, poll_id: int, *, voters: bool, now: dt.datetime | None = None
) -> Poll:
    """Show the result on the voter page (FR-36). The projection screen is a separate choice
    (``show_on_screen``)."""
    now = now or utcnow()
    poll = get_poll(db, owner, poll_id)
    if poll.status not in (PollStatus.CLOSED, PollStatus.PUBLISHED):
        raise InvalidStateError(f"poll_{poll.status.value}")
    if not voters:
        raise InvalidInputError("no_target")
    poll.status = PollStatus.PUBLISHED
    poll.shown_to_voters = True
    if poll.result is not None and poll.result.published_at is None:
        poll.result.published_at = now
    audit.record(
        db,
        "poll.published",
        user_id=owner.id,
        assembly_id=poll.assembly_id,
        details={"poll_id": poll.id},
        now=now,
    )
    return poll


def dismiss_result(
    db: Session, owner: User, poll_id: int, *, now: dt.datetime | None = None
) -> Poll:
    """Hide a published result from the voter page; the projection screen keeps its own
    choice (``show_on_screen``)."""
    poll = get_poll(db, owner, poll_id)
    if poll.status is not PollStatus.PUBLISHED:
        raise InvalidStateError(f"poll_{poll.status.value}")
    poll.shown_to_voters = False
    audit.record(
        db,
        "poll.dismissed",
        user_id=owner.id,
        assembly_id=poll.assembly_id,
        details={"poll_id": poll.id},
        now=now,
    )
    return poll


def show_on_screen(
    db: Session, owner: User, poll_id: int, shown: bool, *, now: dt.datetime | None = None
) -> Poll:
    """Show the result of a closed poll on the projection screen, or take it off (FR-75).

    Off by default and independent of the voter page (FR-36): a result may be on the screen
    without being published, and the other way round. Never an open poll (D4) nor a cancelled
    one (FR-72). Asking for the current state changes nothing and logs nothing; each display
    and each removal is logged with the poll's id only (FR-80).
    """
    now = now or utcnow()
    poll = get_poll(db, owner, poll_id)
    if poll.status not in (PollStatus.CLOSED, PollStatus.PUBLISHED) or poll.result is None:
        raise InvalidStateError(f"poll_{poll.status.value}")
    if poll.shown_on_screen is shown:
        return poll
    poll.shown_on_screen = shown
    poll.screen_shown_at = now if shown else None
    audit.record(
        db,
        "poll.screen_shown" if shown else "poll.screen_hidden",
        user_id=owner.id,
        assembly_id=poll.assembly_id,
        details={"poll_id": poll.id},
        now=now,
    )
    return poll
