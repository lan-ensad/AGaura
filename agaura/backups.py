"""Database backups (SPEC §9.2, §10): compacted copies, never taken while a real poll is open.

``write_copy`` is the only way the application copies its database: ``agaura db backup`` (hence
``deploy/update.sh``) and the automatic backups go through it. ``AutoBackup`` is the background
job of the web process: when ``AGAURA_BACKUP_DIR`` is set, it writes a copy there once a day and
after each poll closing, as soon as no poll of a real session is open, and removes the copies
older than ``AGAURA_BACKUP_KEEP_DAYS`` (always keeping the latest one). The directory lies outside
the database volume (``compose.yaml``). A copy holds the whole database: session passwords and
links stay encrypted with the secret key, which is never in it.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agaura.db import Database
from agaura.models import Assembly, Poll, PollStatus, utcnow

logger = logging.getLogger(__name__)

DAILY = dt.timedelta(days=1)
INTERVAL = 60.0  # seconds between two checks of the background job
NAME = re.compile(r"agaura-(\d{8}-\d{6})Z\.db")
TIME_FORMAT = "%Y%m%d-%H%M%S"
PARTIAL = ".partial"  # a copy being written; renamed once complete and on disk
SPARE_SPACE = 256 * 1024 * 1024  # bytes left free besides room for the copy and as much again
FILE_MODE = 0o600
REPEAT_ERROR = dt.timedelta(hours=6)  # a failure that lasts is logged again this often


class BackupRefusedError(Exception):
    """A poll of a real session is open: copies taken during a vote, compared with each other,
    show which ballots and which participations arrived in between (SPEC §9.2)."""

    def __init__(self, open_polls: int) -> None:
        super().__init__(f"{open_polls} open poll(s)")
        self.open_polls = open_polls


class BackupSpaceError(Exception):
    """Not enough free space: a copy must never take the room the next ballots need."""


def open_polls(db: Session) -> int:
    """Open polls of real sessions; those of demo sessions (FR-09a), which may stay open for
    hours any day, never hold a backup back."""
    query = (
        select(func.count())
        .select_from(Poll)
        .join(Assembly, Assembly.id == Poll.assembly_id)
        .where(Poll.status == PollStatus.OPEN, Assembly.is_demo.is_(False))
    )
    return db.scalar(query) or 0


def open_polls_in_file(path: Path) -> int:
    """``open_polls`` in a copy, read without changing the file."""
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        query = (
            "SELECT count(*) FROM poll JOIN assembly ON assembly.id = poll.assembly_id"
            " WHERE poll.status = ? AND NOT assembly.is_demo"
        )
        return int(connection.execute(query, (PollStatus.OPEN.value,)).fetchone()[0])
    finally:
        connection.close()


def write_copy(database: Database, destination: Path, *, force: bool = False) -> int:
    """Write a compacted copy of the database (``Database.backup``) to ``destination``, which
    must not exist; returns the number of open polls of real sessions in the copy (0 unless
    ``force``).

    Refused with ``BackupRefusedError`` while such a poll is open, unless ``force`` (an
    emergency copy). The copy itself is checked as well, since a poll may open while it is
    written; a refused copy is removed.
    """
    if not force:
        with database.read() as db:
            count = open_polls(db)
        if count:
            raise BackupRefusedError(count)
    database.backup(destination)
    count = open_polls_in_file(destination)
    if count and not force:
        destination.unlink()
        raise BackupRefusedError(count)
    return count


@dataclass(frozen=True, slots=True)
class BackupFile:
    path: Path
    taken_at: dt.datetime  # UTC, to the second
    size: int


def file_name(taken_at: dt.datetime) -> str:
    return f"agaura-{taken_at.astimezone(dt.UTC):{TIME_FORMAT}}Z.db"


def list_backups(directory: Path) -> list[BackupFile]:
    """Automatic copies in ``directory``, oldest first; any other file is left out (and never
    removed: the operator may keep a copy there under another name)."""
    found = []
    for entry in directory.iterdir():
        match = NAME.fullmatch(entry.name)
        if match is None or not entry.is_file():
            continue
        taken_at = dt.datetime.strptime(match[1], TIME_FORMAT).replace(tzinfo=dt.UTC)
        found.append(BackupFile(entry, taken_at, entry.stat().st_size))
    return sorted(found, key=lambda backup: backup.taken_at)


def last_closing(db: Session) -> dt.datetime | None:
    """When the latest poll of a real session stopped taking ballots (closed, or cancelled
    while open)."""
    query = (
        select(func.max(Poll.closed_at))
        .join(Assembly, Assembly.id == Poll.assembly_id)
        .where(Assembly.is_demo.is_(False))
    )
    return db.scalar(query)


def database_size(database: Database) -> int:
    """Bytes of the database file and its WAL."""
    path = Path(database.url.database or "")
    total = 0
    for part in (path, path.with_name(path.name + "-wal")):
        with contextlib.suppress(FileNotFoundError):
            total += part.stat().st_size
    return total


def _sync_directory(directory: Path) -> None:
    """Make a rename in ``directory`` durable."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class AutoBackup:
    """Automatic backups; ``run`` is one check of the background job (``agaura.tasks``)."""

    def __init__(self, database: Database, directory: Path, *, keep_days: int) -> None:
        self.database = database
        self.directory = directory
        self.keep = dt.timedelta(days=keep_days)
        self._failure: tuple[str, dt.datetime] | None = None  # last failure logged, and when
        self._written: tuple[str, dt.datetime] | None = None  # latest copy written, exact time

    def reason(self, db: Session, last: dt.datetime | None, now: dt.datetime) -> str | None:
        """Why a copy is due now, or None: none while a poll of a real session is open; the
        first one, one a day after the latest, and one after any closing since the latest."""
        if open_polls(db):
            return None
        if last is None:
            return "first backup"
        if now - last >= DAILY:
            return "daily"
        closing = last_closing(db)
        # A copy written before a restart is known by its name, to the second: a closing in that
        # same second may be in it or not, so it counts as after it (one copy too many rather
        # than one missing).
        if closing is not None and closing >= last:
            return "after a poll closing"
        return None

    def run(self, now: dt.datetime | None = None) -> Path | None:
        """Write a copy if one is due; returns its path. Never raises for a failure of the
        copy: logs it (again every few hours while it lasts) and tries at the next run."""
        now = now or utcnow()
        try:
            written = self._run(now)
        except BackupRefusedError:
            return None  # a poll opened during the copy: after its closing
        except (OSError, sqlite3.Error, BackupSpaceError) as error:
            self._report(f"{type(error).__name__}: {error}", now)
            return None
        if written is not None and self._failure is not None:
            logger.info("automatic backups work again")
            self._failure = None
        return written

    def _report(self, message: str, now: dt.datetime) -> None:
        if self._failure is not None:
            previous, logged_at = self._failure
            if previous == message and now - logged_at < REPEAT_ERROR:
                return
        logger.error("automatic backup failed: %s", message)
        self._failure = (message, now)

    def _run(self, now: dt.datetime) -> Path | None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._remove_partials()
        backups = list_backups(self.directory)
        # A copy dated later than now (the clock was set back) is kept but never taken as the
        # latest one: it would hold every backup back until that date.
        past = [backup for backup in backups if backup.taken_at <= now]
        last = past[-1].taken_at if past else None
        if past and self._written is not None and past[-1].path.name == self._written[0]:
            last = self._written[1]
        with self.database.read() as db:
            reason = self.reason(db, last, now)
        if reason is None:
            return None
        self._check_space()
        path = self.directory / file_name(now)
        partial = path.with_name(path.name + PARTIAL)
        try:
            write_copy(self.database, partial)
            partial.chmod(FILE_MODE)
            partial.replace(path)
            _sync_directory(self.directory)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        self._written = (path.name, now)
        logger.info("backup written: %s (%s)", path.name, reason)
        self._prune([*backups, BackupFile(path, now, path.stat().st_size)], now)
        return path

    def _check_space(self) -> None:
        size = database_size(self.database)
        free = shutil.disk_usage(self.directory).free
        needed = 2 * size + SPARE_SPACE
        if free < needed:
            mib = 1024 * 1024
            raise BackupSpaceError(
                f"{free // mib} MiB free in {self.directory}, {needed // mib} MiB needed"
            )

    def _remove_partials(self) -> None:
        """Copies left halfway by a stop of the process."""
        for entry in self.directory.glob(f"agaura-*{PARTIAL}"):
            entry.unlink(missing_ok=True)

    def _prune(self, backups: list[BackupFile], now: dt.datetime) -> None:
        """Remove the copies past the retention, never the latest one."""
        for backup in backups[:-1]:
            if now - backup.taken_at > self.keep:
                backup.path.unlink(missing_ok=True)
                logger.info("old backup removed: %s", backup.path.name)
