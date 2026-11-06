import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text
from sqlalchemy.exc import StatementError

from agaura import db as db_module
from agaura.db import Database, alembic_config, compact_after_commit
from agaura.models import Base, Poll, User
from tests.conftest import World


def test_sqlite_settings_on_every_connection(database: Database) -> None:
    with database.read() as db:
        pragma = {
            name: db.execute(text(f"PRAGMA {name}")).scalar()
            for name in (
                "journal_mode",
                "synchronous",
                "foreign_keys",
                "busy_timeout",
                "secure_delete",
            )
        }
    assert pragma == {
        "journal_mode": "wal",
        "synchronous": 2,
        "foreign_keys": 1,
        "busy_timeout": 5000,
        "secure_delete": 1,
    }


def test_migrations_match_the_models(database: Database) -> None:
    with database.engine.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert differences == []


def test_upgrade_creates_the_directory_and_is_idempotent(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'nested' / 'agaura.db'}")
    database.upgrade()
    database.upgrade()
    with database.read() as db:
        head = ScriptDirectory.from_config(alembic_config()).get_current_head()
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == head == "0010"
    database.dispose()


def test_write_commits_and_rolls_back_on_error(database: Database) -> None:
    with database.write() as db:
        db.add(User(email="kept@example.org", password_hash="-"))
    with pytest.raises(RuntimeError), database.write() as db:
        db.add(User(email="lost@example.org", password_hash="-"))
        db.flush()
        raise RuntimeError
    with database.read() as db:
        assert db.scalars(select(User.email)).all() == ["kept@example.org"]


def test_read_never_commits(database: Database) -> None:
    with database.read() as db:
        db.add(User(email="never@example.org", password_hash="-"))
        db.flush()
    with database.read() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0


def test_write_transactions_take_the_write_lock_up_front(database: Database) -> None:
    """Even before writing anything, a write transaction blocks other writers."""
    other = Database(str(database.url))
    raw = other.engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA busy_timeout=0")
        with database.write(), pytest.raises(sqlite3.OperationalError, match="locked"):
            cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("BEGIN IMMEDIATE")  # free again once committed
        cursor.execute("ROLLBACK")
    finally:
        raw.close()
        other.dispose()


def test_migrations_downgrade_and_upgrade_again(database: Database) -> None:
    database.downgrade("base")
    with database.read() as db:
        tables = db.scalars(text("SELECT name FROM sqlite_master WHERE type = 'table'")).all()
    assert set(tables) == {"alembic_version"}
    database.upgrade()


def test_results_flagged_for_the_screen_before_fr75_stay_off(world: World, owner: User) -> None:
    """Before FR-75 ``poll.shown_on_screen`` was written but never read: migration 0006 turns
    it off, so no result appears on a screen without the tribune asking for it."""
    poll_id = world.poll(owner, world.assembly(owner))
    with world.database.write() as db:
        db.get_one(Poll, poll_id).shown_on_screen = True  # as publish_result(screen=True) did
    world.database.downgrade("0005")
    world.database.upgrade()
    with world.database.read() as db:
        poll = db.get_one(Poll, poll_id)
        assert (poll.shown_on_screen, poll.screen_shown_at) == (False, None)


def test_migrations_keep_the_rows_of_rebuilt_tables(world: World, owner: User) -> None:
    """Batch migrations rebuild ``user`` and ``poll`` (copy, DROP, rename): with foreign keys
    on, the DROP would delete every session, option and ballot in cascade."""
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id)
    for token in world.voters(owner, public_id, 3):
        world.vote(public_id, token, poll_id, {"choice": 1})
    tables = ("user", "assembly", "poll", "option", "voter_credential", "participation", "ballot")

    def counts() -> dict[str, int]:
        with world.database.read() as db:
            # Names from the tuple above, never from input.
            return {
                name: db.scalar(text(f'SELECT count(*) FROM "{name}"'))  # noqa: S608
                for name in tables
            }

    before = counts()
    assert before["option"] == 2 and before["ballot"] == 3
    world.database.downgrade("0002")  # before the first migration that rebuilds ``user``
    world.database.upgrade()
    assert counts() == before


def test_naive_datetimes_are_refused(database: Database) -> None:
    with pytest.raises(StatementError, match="naive datetime"), database.write() as db:
        db.add(User(email="a@example.org", password_hash="-", created_at=datetime(2026, 1, 1)))
        db.flush()


def _count_compactions(monkeypatch: pytest.MonkeyPatch) -> list[Database]:
    calls: list[Database] = []
    monkeypatch.setattr(Database, "compact", lambda self: calls.append(self) or True)
    return calls


def test_compaction_runs_after_commit_only_when_requested(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_compactions(monkeypatch)
    with database.write() as db:
        db.add(User(email="a@example.org", password_hash="-"))
    assert calls == []
    with database.write() as db:
        db.add(User(email="b@example.org", password_hash="-"))
        compact_after_commit(db)
    assert calls == [database]
    with pytest.raises(RuntimeError), database.write() as db:
        compact_after_commit(db)
        raise RuntimeError
    assert calls == [database]  # nothing to rebuild after a rollback


def test_compaction_empties_the_wal(database: Database, database_path: Path) -> None:
    with database.write() as db:
        db.add(User(email="a@example.org", password_hash="-"))
    wal = database_path.with_name(database_path.name + "-wal")
    assert wal.stat().st_size > 0
    assert database.compact() is True
    assert wal.stat().st_size == 0
    with database.read() as db:
        assert db.scalars(select(User.email)).all() == ["a@example.org"]


def test_compaction_is_a_no_op_without_a_file() -> None:
    database = Database("sqlite://")
    assert database.compact() is True
    database.dispose()


def test_busy_compaction_warns_and_never_fails_the_request(
    database_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    no_wait = (*db_module._SQLITE_PRAGMAS, "PRAGMA busy_timeout=0")
    monkeypatch.setattr(db_module, "_SQLITE_PRAGMAS", no_wait)
    database = Database(f"sqlite:///{database_path}")
    other = sqlite3.connect(database_path, isolation_level=None)
    caplog.set_level(logging.WARNING, logger="agaura.db")
    try:
        other.execute("BEGIN")
        other.execute("SELECT count(*) FROM user").fetchone()  # a reader pins the WAL
        with database.write() as db:
            db.add(User(email="kept@example.org", password_hash="-"))
            compact_after_commit(db)
        assert "WAL not emptied" in caplog.text
        other.execute("COMMIT")

        other.execute("BEGIN IMMEDIATE")  # a writer holds the lock: VACUUM cannot start
        assert database.compact() is False
        assert "compaction failed (SQLITE_BUSY)" in caplog.text
        other.execute("COMMIT")
        with database.read() as db:
            assert db.scalars(select(User.email)).all() == ["kept@example.org"]
    finally:
        other.close()
        database.dispose()


def test_database_errors_never_carry_query_parameters(database: Database) -> None:
    """A failing INSERT (as during a vote on a full disk) must not put its values, such as a
    ballot's content, into the error message that reaches the logs."""
    marker = "secret-ballot-content"
    with pytest.raises(StatementError) as raised, database.write() as db:
        db.execute(text("INSERT INTO no_such_table (value) VALUES (:value)"), {"value": marker})
    assert marker not in str(raised.value)
