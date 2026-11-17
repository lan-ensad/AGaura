"""Aura battles on the web (SPEC §6.8): the taps of the phones, and the arena kept in step.

``POST /{session_id}/aura/{poll_id}`` receives the taps of one phone in batches, about two a
second while the battle runs (``static/js/aura.js``). The body is ``o=NN&n=NN``: the option's
position and the number of taps, two digits each, so that every request of a battle has the
same size whatever the option (§9.2); the option travels in the body, never in the address,
which the access log of Caddy records. The answer is always empty: 204 once taken (counted or
not: past the allowance of the phone, the taps count for nothing), 401 without a valid voter
credential, 409 when the poll is not a battle under way, 422 for any other body.

The route is ``async`` and reads the database only for the first batch of a phone (the arena
then knows its credential, while a battle of the session runs) or after a restart of the
server, when the arena takes up a battle found open. It never logs, stores or announces
anything: no participation, no ballot, no event to the console (the ticker sends the gauges).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from agaura.aura import Arena
from agaura.db import Database
from agaura.models import AURA, Assembly, Poll, PollStatus, utcnow
from agaura.security import hash_token
from agaura.services import access, assemblies
from agaura.voting import aura
from agaura.web.deps import ArenaDep, CsrfProtected, DatabaseDep, RowId
from agaura.web.security import VOTER_COOKIE, read_cookie
from agaura.web.templating import templates

router = APIRouter()

TAPS = re.compile(rb"o=([0-9]{2})&n=([0-9]{2})")


@dataclass(frozen=True, slots=True)
class _Battle:
    """What the arena needs to follow the battle of an open poll."""

    poll_id: int
    options: tuple[int, ...]
    opened_at: float
    duration: int


def _battle(poll: Poll) -> _Battle | None:
    if not poll.is_aura or poll.status is not PollStatus.OPEN or poll.opened_at is None:
        return None
    return _Battle(
        poll_id=poll.id,
        options=tuple(option.position for option in poll.options),
        opened_at=poll.opened_at.timestamp(),
        duration=aura.parse(range(len(poll.options)), poll.params).duration,
    )


def battle_clock(poll: Poll, now: dt.datetime | None = None) -> dict[str, int]:
    """Milliseconds from now to the tap phase and to the end of a battle (never below 0), for
    the phone: it counts from the moment the page arrives, never with its own clock."""
    now = now or utcnow()
    opened = poll.opened_at or now
    tap_start = opened + dt.timedelta(seconds=aura.CHOOSE_SECONDS)
    end = poll.closes_at or tap_start

    def until(moment: dt.datetime) -> int:
        return max(0, int((moment - now).total_seconds() * 1000))

    return {"choose": until(tap_start), "end": until(end)}


templates.env.globals["battle_clock"] = battle_clock


def _start(arena: Arena, public_id: str, battle: _Battle) -> None:
    arena.start(battle.poll_id, public_id, battle.options, battle.opened_at, battle.duration)


def sync_battles(database: Database, arena: Arena, public_id: str) -> None:
    """Start the battles of a session just pushed, and forget those closed or cancelled: after
    every console action that changes the polls open (``run_action``)."""
    with database.read() as db:
        rows = db.scalars(
            select(Poll)
            .join(Assembly, Assembly.id == Poll.assembly_id)
            .options(selectinload(Poll.options))
            .where(
                Assembly.public_id == public_id,
                Poll.status == PollStatus.OPEN,
                Poll.method == AURA,
            )
        ).all()
        battles = [battle for poll in rows if (battle := _battle(poll)) is not None]
    for battle in battles:
        _start(arena, public_id, battle)
    running = {battle.poll_id for battle in battles}
    for poll_id in arena.polls_of(public_id):
        if poll_id not in running:
            arena.end(poll_id)


def _recognize(
    database: Database, arena: Arena, public_id: str, token: str, digest: str, poll_id: int
) -> int | None:
    """The credential id behind the cookie, the battle taken up if the arena lost it; None
    without a valid credential. Runs in a worker thread."""
    with database.read() as db:
        credential = access.credential_for(db, public_id, token)
        if credential is None:
            return None
        poll = db.get(Poll, poll_id)
        battle = None
        if poll is not None and poll.assembly_id == credential.assembly_id:
            battle = _battle(poll)
        credential_id = credential.id
    if battle is not None:
        _start(arena, public_id, battle)
    arena.remember_phone(public_id, digest, credential_id)
    return credential_id


def _empty(status_code: int) -> Response:
    return Response(status_code=status_code, headers={"Cache-Control": "no-store"})


@router.post("/{session_id}/aura/{poll_id}", dependencies=[CsrfProtected])
async def tap(
    request: Request,
    session_id: str,
    poll_id: RowId,
    database: DatabaseDep,
    arena: ArenaDep,
) -> Response:
    """A batch of taps of one phone during an aura battle (see the module docstring)."""
    public_id = assemblies.normalize_public_id(session_id)
    found = TAPS.fullmatch(await request.body())
    if public_id is None or found is None:
        return _empty(422)
    option, count = int(found[1]), int(found[2])
    token = read_cookie(request, VOTER_COOKIE)
    if not token:
        return _empty(401)
    digest = hash_token(token)
    phone = arena.phone(public_id, digest)
    battle = arena.battle(poll_id)
    if phone is None or battle is None:
        phone = await run_in_threadpool(
            _recognize, database, arena, public_id, token, digest, poll_id
        )
        if phone is None:
            return _empty(401)
        battle = arena.battle(poll_id)
    if battle is None or battle.public_id != public_id or not battle.accepts(arena.clock()):
        return _empty(409)
    arena.tap(poll_id, phone, option, count)
    return _empty(204)
