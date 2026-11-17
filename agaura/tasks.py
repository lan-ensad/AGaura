"""Background jobs run by the web process: poll timers, email delivery, ended demos, automatic
backups, retention."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable

from starlette.concurrency import run_in_threadpool

from agaura import aura, backups
from agaura.aura import Arena
from agaura.config import EmailBackend, Settings
from agaura.db import Database
from agaura.services import demo, outbox, polls, retention
from agaura.web.realtime import Hub, announce_deleted, live_channel, manage_channel

logger = logging.getLogger(__name__)

DEMO_PURGE_INTERVAL = 60.0  # seconds: a demo outlives its login by a minute at most
RETENTION_INTERVAL = 3600.0  # seconds: expired rows and inactive accounts (D12) go within an hour


def make_sender(settings: Settings) -> outbox.EmailSender:
    if settings.email_backend is EmailBackend.SMTP:
        return outbox.SmtpSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password.get_secret_value(),
        )
    return outbox.ConsoleSender(show_body=not settings.is_production)


def close_expired_polls(
    database: Database,
    hub: Hub | None = None,
    arena: Arena | None = None,
    *,
    now: dt.datetime | None = None,
) -> int:
    """Close the polls whose timer ran out, and the aura battles over, with their taps."""
    taps_of = arena.result if arena else polls.no_taps
    with database.write() as db:
        closed = polls.close_expired_polls(db, taps_of=taps_of, now=now)
        public_ids = {poll.assembly.public_id for poll in closed}
        battles = [poll.id for poll in closed if poll.is_aura]
    if arena is not None:
        for poll_id in battles:
            arena.end(poll_id)
    if hub is not None:
        for public_id in public_ids:
            hub.publish(live_channel(public_id), "update", "polls")
            hub.publish(manage_channel(public_id), "turnout", "polls")
    return len(closed)


def purge_demos(database: Database, hub: Hub | None = None) -> int:
    """Delete the demo accounts whose login has ended (FR-09a); returns how many sessions went
    with them."""
    with database.write() as db:
        public_ids = demo.purge(db)
    if hub is not None:
        announce_deleted(hub, public_ids)
    return len(public_ids)


def apply_retention(database: Database, base_url: str) -> int:
    """Purge what has expired and apply D12 (``agaura.services.retention``); returns how many
    inactive accounts were deleted. The deletions share one transaction, hence one rebuild of
    the database file, and wait while a poll of a real session is open."""
    with database.write() as db:
        purged = retention.purge_expired(db)
    with database.write() as db:
        warned = retention.warn_inactive(db, base_url)
    deleted = 0
    with database.write() as db:
        while deleted < retention.DELETIONS_PER_RUN:
            user = retention.next_inactive(db)
            if user is None:
                break
            retention.delete_inactive(db, user)
            deleted += 1
    if purged.logins or purged.links or purged.emails or warned or deleted:
        logger.info(
            "retention: %d expired logins, %d email links, %d emails purged; %d inactive "
            "accounts warned, %d deleted",
            purged.logins,
            purged.links,
            purged.emails,
            warned,
            deleted,
        )
    return deleted


async def run_aura_ticker(arena: Arena, hub: Hub, database: Database) -> None:
    """The frames of the aura battles under way, four times a second, to the screens and
    consoles of their session (SPEC §6.8); then the closing of each battle once over, grace
    included, rather than at the next round of ``close_expired_polls``. Idle between battles."""
    next_close = 0.0
    while True:
        try:
            for public_id, frame in arena.frames():
                hub.publish(manage_channel(public_id), "aura", frame, recheck=False)
            now = arena.clock()
            if now >= next_close and arena.due():
                next_close = now + 1.0  # a closing refused (database busy) is tried again
                await run_in_threadpool(close_expired_polls, database, hub, arena)
                arena.prune()
        except Exception:
            logger.exception("aura battles: round failed")
        await asyncio.sleep(aura.TICK if arena.busy else aura.IDLE)


async def run_periodically(name: str, interval: float, job: Callable[[], object]) -> None:
    """Run ``job`` in a worker thread every ``interval`` seconds; failures are logged."""
    while True:
        try:
            await run_in_threadpool(job)
        except Exception:
            logger.exception("background job %s failed", name)
        await asyncio.sleep(interval)


def start_background_jobs(
    settings: Settings, database: Database, hub: Hub | None = None, arena: Arena | None = None
) -> list[asyncio.Task[None]]:
    sender = make_sender(settings)
    jobs = [
        asyncio.create_task(
            run_periodically(
                "poll_timers",
                settings.poll_timer_interval,
                lambda: close_expired_polls(database, hub, arena),
            )
        ),
        asyncio.create_task(
            run_periodically(
                "email_delivery",
                settings.email_delivery_interval,
                lambda: outbox.deliver_pending(database, sender, settings.email_from),
            )
        ),
    ]
    if settings.backup_dir is not None and database.has_file:
        automatic = backups.AutoBackup(
            database, settings.backup_dir, keep_days=settings.backup_keep_days
        )
        jobs.append(
            asyncio.create_task(run_periodically("backups", backups.INTERVAL, automatic.run))
        )
    elif settings.is_production:
        logger.warning("automatic backups are off: AGAURA_BACKUP_DIR is not set")
    if arena is not None and hub is not None:
        jobs.append(asyncio.create_task(run_aura_ticker(arena, hub, database)))
    jobs.append(
        asyncio.create_task(
            run_periodically(
                "retention",
                RETENTION_INTERVAL,
                lambda: apply_retention(database, settings.base_url.rstrip("/")),
            )
        )
    )
    if settings.demo:
        jobs.append(
            asyncio.create_task(
                run_periodically(
                    "demo_purge", DEMO_PURGE_INTERVAL, lambda: purge_demos(database, hub)
                )
            )
        )
    return jobs
