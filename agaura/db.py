"""Database engine, connection settings and transactions.

SQLite settings (SPEC §10) are applied on every connection: WAL, ``synchronous=FULL`` (a
committed ballot is on disk before the voter sees the confirmation), foreign keys, a 5 s busy
timeout, and ``secure_delete`` (deleted content is overwritten, not left in free space).

Write transactions start with ``BEGIN IMMEDIATE``: they take the write lock up front, so two
writers never interleave their reads and writes (double-vote checks, audit chain). Read
transactions use a plain ``BEGIN`` and never block writers.

Compaction (SPEC §9.2): while a poll is open, each vote commits its participation and its ballot
together, and the files keep that arrival order: the WAL holds one commit per vote, and inside
each B-tree page SQLite allocates cells in insertion order, which pairs every ballot with its
participation. Services that end a poll's voting or delete ballots call
``compact_after_commit``; once their transaction has committed, ``Database.write()`` rebuilds
the file in key order and empties the WAL (``Database.compact``).

Audit key: a ``Database`` built with ``audit_key`` (``Settings.audit_key``, derived from the master
secret) puts it in the ``info`` of each of its sessions, where ``agaura.services.audit`` reads it
(``audit_key_of``) to key the hash chain of the log, so services never pass a keyring around.
Without it, everything works except recording or verifying an audit entry, which raises.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

_BEGIN_OPTION = "agaura_begin"
_COMPACT_AFTER_COMMIT = "agaura_compact_after_commit"
_AUDIT_KEY = "agaura_audit_key"
_BEGIN_STATEMENTS = {"deferred": "BEGIN", "immediate": "BEGIN IMMEDIATE"}
_SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=FULL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA secure_delete=ON",
)


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def on_connect(dbapi_connection: Any, _record: Any) -> None:
        # Let SQLAlchemy emit BEGIN itself (see ``on_begin``) instead of the sqlite3 module.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        for pragma in _SQLITE_PRAGMAS:
            cursor.execute(pragma)
        cursor.close()

    @event.listens_for(engine, "begin")
    def on_begin(connection: Connection) -> None:
        mode = connection.get_execution_options().get(_BEGIN_OPTION, "deferred")
        connection.exec_driver_sql(_BEGIN_STATEMENTS[mode])


def compact_after_commit(db: Session) -> None:
    """Have ``Database.write()`` rebuild the database file once this transaction commits.

    For services that end a poll's voting (closing, cancelling) or delete ballots (session or
    account deletion). Nothing happens if the transaction rolls back.
    """
    db.info[_COMPACT_AFTER_COMMIT] = True


def cancel_compaction(db: Session) -> None:
    """Take back ``compact_after_commit`` for this transaction: what it wrote or deleted needs
    no rebuild now (demo accounts, ``agaura.services.demo``)."""
    db.info.pop(_COMPACT_AFTER_COMMIT, None)


def audit_key_of(db: Session) -> bytes | None:
    """HMAC key of the audit log chain given to the ``Database`` of this session, if any."""
    return db.info.get(_AUDIT_KEY)


class Database:
    def __init__(self, url: str, *, audit_key: bytes | None = None, echo: bool = False) -> None:
        self.url = make_url(url)
        self.is_sqlite = self.url.get_backend_name() == "sqlite"
        self.has_file = self.is_sqlite and self.url.database not in (None, "", ":memory:")
        connect_args = {"check_same_thread": False} if self.is_sqlite else {}
        # hide_parameters: a database error (disk full…) during a vote must not write the
        # ballot's INSERT parameters into the error message, hence the logs.
        self.engine = create_engine(url, echo=echo, connect_args=connect_args, hide_parameters=True)
        if self.is_sqlite:
            _configure_sqlite(self.engine)
        # Each session copies this ``info`` into its own (``Session.info`` is per session).
        info = {_AUDIT_KEY: audit_key} if audit_key else {}
        self._sessions = sessionmaker(self.engine, expire_on_commit=False, info=info)

    @contextmanager
    def write(self) -> Iterator[Session]:
        """Transaction that may write; committed on success, rolled back on any exception.

        After the commit, runs the compaction requested with ``compact_after_commit``.
        """
        with self._sessions() as session:
            session.connection(execution_options={_BEGIN_OPTION: "immediate"})
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise
            compact = session.info.pop(_COMPACT_AFTER_COMMIT, False)
        if compact:
            self.compact()

    @contextmanager
    def read(self) -> Iterator[Session]:
        """Read-only transaction, never committed.

        Closing the session ends the transaction without expiring the loaded objects, so they
        stay readable (detached) after the block, e.g. by templates.
        """
        with self._sessions() as session:
            yield session

    def compact(self) -> bool:
        """Rebuild the database file so that its physical layout reflects keys only.

        ``VACUUM`` rewrites every table and index in key order (a ballot's key is its random
        id), then a ``TRUNCATE`` checkpoint copies the result into the main file and empties the
        WAL. Runs on its own connection, outside any transaction. Never raises: when the
        database is busy, logs a warning (no data in it) and returns False; the next compaction
        rebuilds everything again. A no-op without a database file.
        """
        if not self.has_file:
            return True
        try:
            raw = self.engine.raw_connection()
            try:
                cursor = raw.cursor()
                cursor.execute("VACUUM")
                busy, _, _ = cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                cursor.close()
            finally:
                raw.close()
        except (sqlite3.Error, DBAPIError) as error:
            cause = getattr(error, "orig", None) or error
            name = getattr(cause, "sqlite_errorname", None) or type(cause).__name__
            logger.warning("database compaction failed (%s)", name)
            return False
        if busy:
            logger.warning("database compaction: WAL not emptied, readers still active")
            return False
        return True

    def backup(self, destination: Path) -> None:
        """Write a compacted copy of the database to ``destination``, which must not exist.

        ``VACUUM INTO`` copies one consistent snapshot, every table and index in key order;
        unlike a page-by-page copy (backup API, ``sqlite3 .backup``), it never reproduces the
        layout of pages written while a poll was open. The copy is flushed to disk before
        returning.
        """
        raw = self.engine.raw_connection()
        try:
            cursor = raw.cursor()
            cursor.execute("VACUUM INTO ?", (str(destination),))
            cursor.close()
        finally:
            raw.close()
        with destination.open("rb") as copy:  # SQLite does not sync VACUUM INTO output
            os.fsync(copy.fileno())

    def upgrade(self, revision: str = "head") -> None:
        """Create the database file if needed and apply every pending migration."""
        if self.is_sqlite and self.url.database:
            Path(self.url.database).parent.mkdir(parents=True, exist_ok=True)
        self._migrate(command.upgrade, revision)

    def downgrade(self, revision: str) -> None:
        """Undo migrations down to ``revision`` (tests, recovery)."""
        self._migrate(command.downgrade, revision)

    def _migrate(self, step: Callable[[Config, str], None], revision: str) -> None:
        with self.engine.connect() as connection, migration_transaction(connection):
            step(alembic_config(connection), revision)

    def dispose(self) -> None:
        self.engine.dispose()


@contextmanager
def migration_transaction(connection: Connection) -> Iterator[None]:
    """One transaction for migrations, with SQLite's foreign keys off.

    Batch migrations rebuild a table SQLite cannot alter in place: copy, DROP, rename. With
    foreign keys on, the DROP deletes the rows that refer to the table in cascade (every session
    for ``user``, every option and ballot for ``poll``). The pragma only takes effect outside a
    transaction; the keys are checked before the commit instead.
    """
    driver = (
        connection.connection.driver_connection if connection.dialect.name == "sqlite" else None
    )
    if driver is not None:
        driver.execute("PRAGMA foreign_keys=OFF")
    try:
        with connection.begin():
            yield
            if driver is not None:
                broken = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
                if broken:
                    raise RuntimeError(f"migration broke foreign keys: {broken[:5]}")
    finally:
        if driver is not None:
            driver.execute("PRAGMA foreign_keys=ON")


def alembic_config(connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", "agaura:migrations")
    config.attributes["connection"] = connection
    return config
