"""Automatic backups (SPEC §10): once a day and after each poll closing, never during a vote,
kept 30 days."""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from pathlib import Path

import pytest

from agaura import backups
from agaura.backups import AutoBackup, BackupFile
from agaura.models import User, utcnow
from agaura.services import demo, polls
from tests.conftest import World

NOW = dt.datetime(2026, 11, 5, 14, 30, 0, tzinfo=dt.UTC)


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    return tmp_path / "backups"


@pytest.fixture
def job(world: World, directory: Path) -> AutoBackup:
    return AutoBackup(world.database, directory, keep_days=30)


def ballots_in(path: Path) -> int:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        return int(connection.execute("SELECT count(*) FROM ballot").fetchone()[0])
    finally:
        connection.close()


def closed_poll(world: World, owner: User, public_id: str, voters: int = 2) -> int:
    poll_id = world.poll(owner, public_id)
    for token in world.voters(owner, public_id, voters):
        world.vote(public_id, token, poll_id, {"choice": 1})
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
    return poll_id


def test_first_backup_is_a_private_compacted_copy(job: AutoBackup, directory: Path) -> None:
    path = job.run(NOW)
    assert path == directory / "agaura-20261105-143000Z.db"
    assert path.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700  # created by the job when missing
    assert ballots_in(path) == 0
    assert [entry.name for entry in directory.iterdir()] == [path.name]  # no partial file left
    assert job.run(NOW + dt.timedelta(minutes=1)) is None  # nothing since


def test_a_copy_after_each_poll_closing_and_once_a_day(
    job: AutoBackup, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    job.run(NOW)
    with world.database.read() as db:
        assert backups.last_closing(db) is None
    closed_poll(world, owner, public_id)  # closed now, after NOW
    after_closing = job.run(utcnow())
    assert after_closing is not None and ballots_in(after_closing) == 2
    later = utcnow() + dt.timedelta(hours=23)
    assert job.run(later) is None
    daily = job.run(later + dt.timedelta(hours=1, seconds=1))
    assert daily is not None and daily != after_closing


def test_never_while_a_poll_of_a_real_session_is_open(
    job: AutoBackup, world: World, owner: User, directory: Path
) -> None:
    """Two copies taken during a vote would show which ballots arrived between them (§9.2): the
    copy waits for the closing, even when a daily one is due."""
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id)
    assert job.run(NOW) is None
    assert not list(directory.iterdir())
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
    assert job.run(utcnow()) is not None


def test_demo_sessions_neither_hold_back_nor_trigger_a_copy(job: AutoBackup, world: World) -> None:
    with world.database.write() as db:
        visitor = demo.start(db, world.keyring, limit=1)
    public_id = world.assembly(visitor)
    open_poll = world.poll(visitor, public_id)
    # The real clock, as the polls below close at its time: from NOW, a fixed date, the daily
    # copy would be due again a day later, whatever the closing.
    start = utcnow()
    assert job.run(start) is not None  # a demo poll is open: no reason to wait
    closed_poll(world, visitor, public_id)
    assert open_poll and job.run(utcnow()) is None  # a demo poll closed: no copy for it


def test_a_poll_opened_during_the_copy_removes_it(
    job: AutoBackup,
    world: World,
    owner: User,
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, open_it=False)
    copy_database = type(world.database).backup

    def open_then_copy(database: object, destination: Path) -> None:
        with world.database.write() as db:
            polls.open_poll(db, owner, poll_id)
        copy_database(database, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(type(world.database), "backup", open_then_copy)
    assert job.run(NOW) is None
    assert not list(directory.iterdir())


def test_copies_past_the_retention_are_removed_but_never_the_latest(
    job: AutoBackup, directory: Path
) -> None:
    directory.mkdir(mode=0o700)
    names = [backups.file_name(NOW - dt.timedelta(days=days)) for days in (40, 31, 29, 2)]
    for name in names:
        (directory / name).write_bytes(b"")
    (directory / "kept-by-hand.db").write_bytes(b"")
    path = job.run(NOW)
    assert path is not None
    assert sorted(entry.name for entry in directory.iterdir()) == sorted(
        [names[2], names[3], path.name, "kept-by-hand.db"]
    )
    job.keep = dt.timedelta(days=1)
    job._prune([BackupFile(path, NOW, 0)], NOW + dt.timedelta(days=90))  # alone: kept
    assert path.exists()


def test_a_copy_dated_later_holds_nothing_back(job: AutoBackup, directory: Path) -> None:
    """After the clock was set back, the copy named later is kept, and backups go on."""
    directory.mkdir(mode=0o700)
    future = directory / backups.file_name(NOW + dt.timedelta(days=3))
    future.write_bytes(b"")
    assert job.run(NOW) is not None
    assert job.run(NOW + dt.timedelta(minutes=1)) is None
    assert future.exists()


def test_a_copy_left_halfway_is_removed(job: AutoBackup, directory: Path) -> None:
    directory.mkdir(mode=0o700)
    partial = directory / ("agaura-20261101-000000Z.db" + backups.PARTIAL)
    partial.write_bytes(b"SQLite")
    job.run(NOW)
    assert not partial.exists()


def test_list_backups_reads_the_names(directory: Path) -> None:
    directory.mkdir()
    (directory / "agaura-20261105-143000Z.db").write_bytes(b"1234")
    (directory / "agaura-20261104-090000Z.db").write_bytes(b"")
    (directory / "agaura-2026-11-05.db").write_bytes(b"")  # another name: left alone
    (directory / "agaura-20261103-090000Z.db").mkdir()
    found = backups.list_backups(directory)
    assert [(backup.taken_at, backup.size) for backup in found] == [
        (dt.datetime(2026, 11, 4, 9, 0, tzinfo=dt.UTC), 0),
        (NOW, 4),
    ]


def test_a_failure_is_logged_once_then_again_hours_later(
    job: AutoBackup, directory: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory.write_text("not a directory")
    with caplog.at_level(logging.ERROR, logger="agaura.backups"):
        assert job.run(NOW) is None
        assert job.run(NOW + dt.timedelta(minutes=1)) is None
        assert len(caplog.records) == 1
        assert job.run(NOW + backups.REPEAT_ERROR) is None
        assert len(caplog.records) == 2
    assert "automatic backup failed: FileExistsError" in caplog.records[0].getMessage()
    directory.unlink()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="agaura.backups"):
        assert job.run(NOW + backups.REPEAT_ERROR) is not None
    assert "automatic backups work again" in caplog.text


def test_no_copy_without_room_for_the_next_ballots(
    job: AutoBackup,
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The copy usually shares the disk of the database: it must never fill it before a vote."""
    real_usage = backups.shutil.disk_usage

    def nearly_full(path: Path) -> object:
        return real_usage(path)._replace(free=backups.SPARE_SPACE)

    monkeypatch.setattr(backups.shutil, "disk_usage", nearly_full)
    with caplog.at_level(logging.ERROR, logger="agaura.backups"):
        assert job.run(NOW) is None
    assert "BackupSpaceError" in caplog.text and "MiB needed" in caplog.text
    assert list(directory.iterdir()) == []


def test_database_size_counts_the_wal(world: World, owner: User) -> None:
    assert backups.database_size(world.database) > 0
