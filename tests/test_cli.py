"""Operator command line: accounts, super-admins, quotas, session deletion, statistics,
backups."""

from __future__ import annotations

import datetime as dt
import io
import random
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from agaura import cli
from agaura.config import get_settings
from agaura.db import Database
from agaura.models import Assembly, AuditEntry, User, UserSession
from agaura.security import verify_password
from agaura.services import accounts, auth, demo, outbox, polls
from tests.conftest import TEST_SECRET_KEY, World

PASSWORD = "correct horse battery"

Run = Callable[..., tuple[int, str, str]]


@pytest.fixture
def run(
    database_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Iterator[Run]:
    monkeypatch.setenv("AGAURA_DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("AGAURA_SECRET_KEY", TEST_SECRET_KEY)  # the audit key of ``world``
    get_settings.cache_clear()

    def invoke(*argv: str, stdin: str | None = None) -> tuple[int, str, str]:
        if stdin is not None:
            monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        status = cli.main(list(argv))
        captured = capsys.readouterr()
        return status, captured.out, captured.err

    yield invoke
    get_settings.cache_clear()


def user(database: Database, email: str) -> User:
    with database.read() as db:
        found = db.scalar(select(User).where(User.email == email))
        assert found is not None
        return found


def operator_actions(database: Database) -> list[str]:
    with database.read() as db:
        return [
            entry.action
            for entry in db.scalars(select(AuditEntry).where(AuditEntry.actor == "operator"))
        ]


class TestUserCreate:
    def test_from_stdin(self, run: Run, database: Database) -> None:
        status, out, _ = run(
            "user",
            "create",
            "Ops@Example.org",
            "--verified",
            "--name",
            "Camille",
            "--password-stdin",
            stdin=PASSWORD + "\n",
        )
        assert status == 0 and "ops@example.org created (email confirmed)" in out
        created = user(database, "ops@example.org")
        assert created.display_name == "Camille" and created.email_verified_at is not None
        assert verify_password(created.password_hash, PASSWORD)
        assert operator_actions(database) == ["account.created"]

    def test_with_getpass(
        self, run: Run, database: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": PASSWORD)
        status, out, _ = run("user", "create", "ops@example.org")
        assert status == 0 and "email not confirmed" in out
        assert user(database, "ops@example.org").email_verified_at is None

    def test_getpass_mismatch(self, run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
        answers = iter([PASSWORD, "something else"])
        monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
        status, _, err = run("user", "create", "ops@example.org")
        assert status == 1 and "do not match" in err

    @pytest.mark.parametrize(
        ("email", "password", "message"),
        [
            ("ops@example.org", "short", "at least 12 characters"),
            ("not-an-email", PASSWORD, "Invalid email address"),
            ("a@x.org,b@y.org", PASSWORD, "Invalid email address"),
            ("x\n@y.org", PASSWORD, "Invalid email address"),
        ],
    )
    def test_invalid(self, run: Run, email: str, password: str, message: str) -> None:
        status, _, err = run("user", "create", email, "--password-stdin", stdin=password)
        assert status == 1 and message in err

    def test_taken(self, run: Run) -> None:
        run("user", "create", "ops@example.org", "--password-stdin", stdin=PASSWORD)
        status, _, err = run(
            "user", "create", "ops@example.org", "--password-stdin", stdin=PASSWORD
        )
        assert status == 1 and "already uses" in err

    def test_superadmin(self, run: Run, database: Database) -> None:
        status, out, _ = run(
            "user", "create", "Root@Example.org", "--superadmin", "--password-stdin", stdin=PASSWORD
        )
        assert status == 0 and "Super-admin root@example.org created" in out
        created = user(database, "root@example.org")
        assert created.is_superadmin and created.email_verified_at is not None
        assert operator_actions(database) == ["account.created"]


class TestUserDelete:
    def test_deletes_the_account_and_its_sessions(
        self, run: Run, world: World, owner: User, database: Database
    ) -> None:
        world.assembly(owner, in_session=False)
        status, out, _ = run("user", "delete", "Owner@example.org")
        assert status == 0 and "owner@example.org deleted with its 1 session(s)" in out
        with database.read() as db:
            assert db.scalar(select(User)) is None and db.scalar(select(Assembly)) is None
        assert operator_actions(database) == ["account.deleted"]

    def test_superadmin(self, run: Run, database: Database) -> None:
        with database.write() as db:
            accounts.create_user_by_operator(db, "root@example.org", PASSWORD, superadmin=True)
        status, out, _ = run("user", "delete", "root@example.org")
        assert status == 0 and "deleted with its 0 session(s)" in out

    def test_refused_while_in_session(
        self, run: Run, world: World, owner: User, database: Database
    ) -> None:
        world.assembly(owner, in_session=True)
        status, _, err = run("user", "delete", owner.email)
        assert status == 1 and "in session" in err and "--force" in err
        assert user(database, owner.email).id == owner.id

    def test_unknown(self, run: Run) -> None:
        status, _, err = run("user", "delete", "x@example.org")
        assert status == 1 and "No such user" in err


def test_disable_and_enable(run: Run, database: Database) -> None:
    with database.write() as db:
        account = accounts.create_user(db, "a@example.org", PASSWORD, verified=True)
        token = auth.start_session(db, account)
    status, out, _ = run("user", "disable", "A@example.org")
    assert status == 0 and "a@example.org disabled" in out
    assert user(database, "a@example.org").disabled_at is not None
    with database.read() as db:
        assert db.scalar(select(UserSession)) is None
        assert auth.lookup(db, token) is None
        assert auth.authenticate(db, "a@example.org", PASSWORD) is None
    status, out, _ = run("user", "enable", "a@example.org")
    assert status == 0 and "enabled" in out
    with database.write() as db:
        assert auth.authenticate(db, "a@example.org", PASSWORD) is not None
    assert operator_actions(database) == ["account.disabled", "account.enabled"]


def test_unknown_account(run: Run) -> None:
    for argv in (("user", "disable", "x@example.org"), ("user", "quota", "x@example.org")):
        status, _, err = run(*argv)
        assert status == 1 and "No such user" in err


def test_quota(run: Run, world: World, owner: User, database: Database) -> None:
    status, out, _ = run("user", "quota", owner.email)
    assert status == 0 and "open_assemblies: 20" in out
    status, out, _ = run(
        "user", "quota", owner.email, "--open-assemblies", "3", "--codes-per-assembly", "5000"
    )
    assert status == 0 and "open_assemblies: 3" in out and "codes_per_assembly: 5000" in out
    assert accounts.quotas(user(database, owner.email)) == accounts.Quotas(
        open_assemblies=3, codes_per_assembly=5000
    )
    status, out, _ = run("user", "quota", owner.email, "--reset", "--polls-per-assembly", "10")
    assert accounts.quotas(user(database, owner.email)) == accounts.Quotas(polls_per_assembly=10)
    status, _, err = run("user", "quota", owner.email, "--open-assemblies", "-1")
    assert status == 1 and "Invalid quota" in err
    assert operator_actions(database) == ["account.quotas_changed"] * 2


class TestSessionDelete:
    def test_refused_while_in_session_unless_forced(
        self, run: Run, world: World, owner: User, database: Database
    ) -> None:
        public_id = world.assembly(owner, in_session=True)
        world.poll(owner, public_id)
        status, _, err = run("session", "delete", public_id.lower())
        assert status == 1 and "in session" in err and "--force" in err
        status, out, _ = run("session", "delete", public_id.lower(), "--force")
        assert status == 0 and f"Session {public_id} deleted." in out
        with database.read() as db:
            assert db.scalar(select(Assembly)) is None
        assert operator_actions(database) == ["assembly.deleted"]

    def test_closed_session(self, run: Run, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False)
        assert run("session", "delete", public_id)[0] == 0

    def test_unknown_session(self, run: Run) -> None:
        for public_id in ("ZZZZ99", "nope"):
            status, _, err = run("session", "delete", public_id)
            assert status == 1 and "No such assembly" in err


def test_stats(run: Run, world: World, owner: User) -> None:
    public_id = world.assembly(owner, in_session=True)
    world.assembly(owner, in_session=False)
    poll_id = world.poll(owner, public_id)
    for token in world.voters(owner, public_id, 3):
        world.vote(public_id, token, poll_id, {"choice": 1})
    with world.database.write() as db:
        root = accounts.create_user_by_operator(db, "root@example.org", PASSWORD, superadmin=True)
        accounts.invite(db, root, "invited@example.org")
    status, out, _ = run("stats")
    assert status == 0
    lines = out.splitlines()
    for expected in [
        "accounts: 3",
        "  verified: 1",
        "  disabled: 0",
        "  invitations pending: 1",
        "  super-admins: 1",
        "sessions: 2",
        "  preparation: 1",
        "  in_session: 1",
        "  closed: 0",
        "demo sessions: 0",
        "polls: 1",
        "ballots: 3",
    ]:
        assert expected in lines, expected


def test_stats_count_demo_sessions_apart(run: Run, world: World, owner: User) -> None:
    """``in_session`` is the deployment guard (deploy/update.sh, SPEC §10), as on /health: a
    demo (FR-09a) in session never holds an update back."""
    world.assembly(owner, in_session=False)
    with world.database.write() as db:
        visitor = demo.start(db, world.keyring, limit=1)
    world.assembly(visitor)  # in session, besides its example sessions in preparation and closed
    status, out, _ = run("stats")
    assert status == 0
    lines = out.splitlines()
    for expected in ["sessions: 1", "  preparation: 1", "  in_session: 0", "demo sessions: 3"]:
        assert expected in lines, expected


def test_audit_and_db_commands_still_work(run: Run) -> None:
    assert run("db", "upgrade")[0] == 0
    status, out, _ = run("audit", "verify")
    assert status == 0 and "0 entries, chain intact" in out
    # Operator entries are keyed with AGAURA_SECRET_KEY, like the application's.
    assert run("user", "create", "op@example.org", "--password-stdin", stdin=PASSWORD)[0] == 0
    status, out, _ = run("audit", "verify")
    assert status == 0 and "1 entries, chain intact" in out


class TestDbBackup:
    def test_writes_a_usable_copy(
        self, run: Run, world: World, owner: User, tmp_path: Path
    ) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id)
        for token in world.voters(owner, public_id, 3):
            world.vote(public_id, token, poll_id, {"choice": 1})
        with world.database.write() as db:
            polls.close_poll(db, owner, poll_id)
        copy = tmp_path / "backup.db"
        status, out, err = run("db", "backup", str(copy))
        assert status == 0 and f"Backup written to {copy}." in out and err == ""
        connection = sqlite3.connect(copy)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert connection.execute("SELECT count(*) FROM ballot").fetchone() == (3,)
        finally:
            connection.close()
        status, _, err = run("db", "backup", str(copy))
        assert status == 1 and "already exists" in err
        status, _, err = run("db", "backup", str(tmp_path / "missing" / "backup.db"))
        assert status == 1 and "Backup failed" in err

    def test_refused_while_a_poll_is_open_unless_forced(
        self, run: Run, world: World, owner: User, tmp_path: Path
    ) -> None:
        world.poll(owner, world.assembly(owner))
        copy = tmp_path / "backup.db"
        status, _, err = run("db", "backup", str(copy))
        assert status == 1 and "A poll is open" in err and "--force" in err
        assert not copy.exists()
        status, out, err = run("db", "backup", str(copy), "--force")
        assert status == 0 and copy.exists() and "Backup written" in out
        assert "1 open poll(s)" in err

    def test_open_polls_of_demo_sessions_do_not_hold_it_back(
        self, run: Run, world: World, tmp_path: Path
    ) -> None:
        """A demo (FR-09a) may keep a poll open for hours, any day: never a reason to skip the
        backup of the real sessions."""
        with world.database.write() as db:
            visitor = demo.start(db, world.keyring, limit=1)
        public_id = world.assembly(visitor)
        world.poll(visitor, public_id)
        status, out, err = run("db", "backup", str(tmp_path / "backup.db"))
        assert status == 0 and "Backup written" in out and err == ""

    def test_refused_when_a_poll_opens_during_the_copy(
        self,
        run: Run,
        world: World,
        owner: User,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, open_it=False)
        copy_database = Database.backup

        def open_then_copy(database: Database, destination: Path) -> None:
            with world.database.write() as db:
                polls.open_poll(db, owner, poll_id)
            copy_database(database, destination)

        monkeypatch.setattr(Database, "backup", open_then_copy)
        copy = tmp_path / "backup.db"
        status, _, err = run("db", "backup", str(copy))
        assert status == 1 and "A poll is open" in err
        assert not copy.exists()


class TestDbBackups:
    def test_off_without_a_directory(self, run: Run) -> None:
        status, out, _ = run("db", "backups")
        assert status == 0 and "Automatic backups are off" in out

    def test_lists_the_automatic_copies(
        self, run: Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = tmp_path / "auto"
        monkeypatch.setenv("AGAURA_BACKUP_DIR", str(directory))
        status, out, _ = run("db", "backups")
        assert status == 0 and "No backup yet" in out
        directory.mkdir()
        (directory / "agaura-20261104-090000Z.db").write_bytes(b"x" * 3 * 1024 * 1024)
        (directory / "agaura-20261105-143000Z.db").write_bytes(b"x" * 2048)
        status, out, _ = run("db", "backups")
        assert status == 0 and "kept 30 days" in out
        lines = out.splitlines()
        assert lines[1] == "  agaura-20261104-090000Z.db  2026-11-04 09:00 UTC  3.0 MiB"
        assert lines[2] == "  agaura-20261105-143000Z.db  2026-11-05 14:30 UTC  2 KiB"
        assert lines[3].startswith("Latest: ") and lines[3].endswith(" ago.")


class TestDbCheck:
    def copy(self, run: Run, tmp_path: Path) -> Path:
        path = tmp_path / "copy.db"
        assert run("db", "backup", str(path))[0] == 0
        return path

    def test_a_good_copy(self, run: Run, world: World, owner: User, tmp_path: Path) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id)
        for token in world.voters(owner, public_id, 2):
            world.vote(public_id, token, poll_id, {"choice": 1})
        with world.database.write() as db:
            polls.close_poll(db, owner, poll_id)
        path = self.copy(run, tmp_path)
        before = path.read_bytes()
        status, out, err = run("db", "check", str(path))
        assert status == 0 and err == ""
        assert f"{path}: intact, schema " in out
        assert "ballots: 2" in out and "latest poll closing: " in out
        assert "chain intact" in out
        assert path.read_bytes() == before  # only read
        assert sorted(entry.name for entry in tmp_path.iterdir() if "copy" in entry.name) == [
            "copy.db"
        ]

    def test_an_older_schema_is_migrated_on_a_temporary_copy(
        self, run: Run, tmp_path: Path
    ) -> None:
        old = tmp_path / "old.db"
        database = Database(f"sqlite:///{old}")
        database.upgrade("0008")
        database.dispose()
        status, out, _ = run("db", "check", str(old))
        assert status == 0 and "schema 0008" in out and "chain intact" in out

    @pytest.mark.parametrize(
        ("content", "message"),
        [(None, "No file"), (b"hello", "not an SQLite database")],
    )
    def test_not_a_database(
        self, run: Run, tmp_path: Path, content: bytes | None, message: str
    ) -> None:
        path = tmp_path / "file.db"
        if content is not None:
            path.write_bytes(content)
        status, _, err = run("db", "check", str(path))
        assert status == 1 and message in err

    def test_a_damaged_copy(self, run: Run, tmp_path: Path) -> None:
        path = self.copy(run, tmp_path)
        data = bytearray(path.read_bytes())
        page = 4096
        data[2 * page + 8 : 3 * page] = b"\xff" * (page - 8)  # a page of the tables, garbled
        path.write_bytes(bytes(data))
        status, _, err = run("db", "check", str(path))
        assert status == 1 and ("damaged" in err or "not an AGaura database" in err)

    def test_a_copy_from_a_newer_version(self, run: Run, tmp_path: Path) -> None:
        path = self.copy(run, tmp_path)
        connection = sqlite3.connect(path)
        connection.execute("UPDATE alembic_version SET version_num = '0099'")
        connection.commit()
        connection.close()
        status, _, err = run("db", "check", str(path))
        assert status == 1 and "newer version of AGaura (schema 0099)" in err

    def test_an_audit_log_keyed_with_another_secret(
        self, run: Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert run("user", "create", "op@example.org", "--password-stdin", stdin=PASSWORD)[0] == 0
        path = self.copy(run, tmp_path)
        monkeypatch.setenv("AGAURA_SECRET_KEY", "another-secret-key-0123456789abcdef-012345")
        get_settings.cache_clear()
        status, _, err = run("db", "check", str(path))
        assert status == 1 and "Audit log BROKEN at entry 1" in err

    def test_warns_of_an_open_poll(
        self, run: Run, world: World, owner: User, tmp_path: Path
    ) -> None:
        world.poll(owner, world.assembly(owner))
        path = tmp_path / "forced.db"
        assert run("db", "backup", str(path), "--force")[0] == 0
        status, _, err = run("db", "check", str(path))
        assert status == 0 and "1 open poll(s)" in err


@pytest.mark.parametrize(
    ("minutes", "text"),
    [(-1, "0 min ago"), (59, "59 min ago"), (60 * 47, "47 h ago"), (60 * 24 * 3, "3 days ago")],
)
def test_backup_age(minutes: int, text: str) -> None:
    assert cli._age(dt.timedelta(minutes=minutes)) == text


def test_a_damaged_database_gets_a_message_not_a_traceback(
    run: Run, database_path: Path, tmp_path: Path
) -> None:
    data = database_path.read_bytes()
    database_path.write_bytes(data[:4096] + random.Random(7).randbytes(len(data) - 4096))  # noqa: S311
    for argv in (("stats",), ("db", "backup", str(tmp_path / "copy.db"))):
        status, _, err = run(*argv)
        assert status == 1 and "Traceback" not in err
        assert "malformed" in err
    assert "deploy/restore.sh --force" in run("stats")[2]


def test_email_failed_and_retry(run: Run, database: Database) -> None:
    status, out, _ = run("email", "failed")
    assert status == 0 and out == "No email given up.\n"
    now = dt.datetime(2026, 11, 5, 14, 30, tzinfo=dt.UTC)
    with database.write() as db:
        email = outbox.queue_email(db, "a@example.org", "Bienvenue", "…", now=now)
        email.failed_at, email.attempts, email.last_error = now, 1, "SMTPRecipientsRefused: 550"
        email_id = email.id
    status, out, _ = run("email", "failed")
    assert status == 0
    assert (
        f"  #{email_id}  2026-11-05 14:30 UTC  1 attempt(s)  to a@example.org  'Bienvenue'" in out
    )
    assert "      SMTPRecipientsRefused: 550" in out
    for argv in (("email", "retry"), ("email", "retry", "--all", str(email_id))):
        status, _, err = run(*argv)
        assert status == 1 and "or --all" in err
    status, out, _ = run("email", "retry", str(email_id))
    assert status == 0 and out == "1 email(s) queued again.\n"
    assert run("email", "failed")[1] == "No email given up.\n"
