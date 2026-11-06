import hashlib
import itertools
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import select, text

from agaura import cli
from agaura.config import get_settings
from agaura.db import Database, audit_key_of
from agaura.models import AuditEntry, User
from agaura.services import audit
from tests.conftest import TEST_AUDIT_KEY, TEST_SECRET_KEY, World


@pytest.fixture
def logged(world: World, owner: User) -> list[AuditEntry]:
    public_id = world.assembly(owner)
    world.poll(owner, public_id)
    with world.database.read() as db:
        return list(db.scalars(select(AuditEntry).order_by(AuditEntry.id)))


def test_entries_are_chained(logged: list[AuditEntry]) -> None:
    assert [entry.action for entry in logged] == [
        "assembly.created",
        "assembly.opened",
        "poll.created",
        "poll.opened",
    ]
    assert logged[0].prev_hash == audit.GENESIS_HASH
    for previous, entry in itertools.pairwise(logged):
        assert entry.prev_hash == previous.hash
        assert entry.hash == audit.entry_hash(entry, TEST_AUDIT_KEY)
    # Keyed: the plain SHA-256 of the same canonical form is not the stored hash.
    assert logged[0].hash != hashlib.sha256(audit._canonical(logged[0])).hexdigest()
    assert len(logged[0].hash) == 64


def test_intact_chain_verifies(world: World, logged: list[AuditEntry]) -> None:
    with world.database.read() as db:
        report = audit.verify_chain(db)
        assert [e.action for e in audit.session_log(db, logged[0].assembly_id or 0)][-1] == (
            "poll.opened"
        )
    assert (report.ok, report.checked) == (True, 4)


@pytest.mark.parametrize(
    "tampering",
    [
        'UPDATE audit_entry SET details = \'{"title": "Autre"}\' WHERE id = 2',
        "UPDATE audit_entry SET at = '2020-01-01 00:00:00.000000' WHERE id = 2",
        "DELETE FROM audit_entry WHERE id = 2",
    ],
)
def test_tampering_is_detected(world: World, logged: list[AuditEntry], tampering: str) -> None:
    with world.database.write() as db:
        db.execute(text(tampering))
    with world.database.read() as db:
        report = audit.verify_chain(db)
    assert not report.ok
    assert report.broken_at in {2, 3}


def rewrite_chain(database: Database, sign: Callable[[AuditEntry], str]) -> None:
    """Change entry 2, then compute every hash again from there, as a forger would."""
    with database.write() as db:
        entries = list(db.scalars(select(AuditEntry).order_by(AuditEntry.id)))
        entries[1].details = {**entries[1].details, "title": "Autre"}
        previous = entries[0].hash
        for entry in entries[1:]:
            entry.prev_hash = previous
            entry.hash = sign(entry)
            previous = entry.hash


@pytest.mark.parametrize(
    "sign",
    [
        pytest.param(
            lambda entry: hashlib.sha256(audit._canonical(entry)).hexdigest(), id="sha256"
        ),
        pytest.param(lambda entry: audit.entry_hash(entry, b"guessed-key" * 3), id="other-key"),
    ],
)
def test_a_chain_rewritten_without_the_key_is_detected(
    world: World, logged: list[AuditEntry], sign: Callable[[AuditEntry], str]
) -> None:
    """A copy of the database file is not enough to rewrite the log (SPEC §9.1)."""
    rewrite_chain(world.database, sign)
    with world.database.read() as db:
        report = audit.verify_chain(db)
    assert (report.ok, report.broken_at, report.checked) == (False, 2, 1)


def test_an_anchor_catches_a_chain_rewritten_with_the_key(
    world: World, logged: list[AuditEntry]
) -> None:
    """With the key, the chain can be rewritten; an anchor kept elsewhere shows it."""
    assembly_id = logged[-1].assembly_id or 0
    with world.database.read() as db:
        anchor = audit.session_anchor(db, assembly_id)
        assert anchor == logged[-1].hash[: audit.ANCHOR_LENGTH]
        assert audit.find_anchor(db, anchor) == logged[-1].id
        assert audit.find_anchor(db, logged[-1].hash) == logged[-1].id
        assert audit.session_anchor(db, assembly_id + 1) is None
    rewrite_chain(world.database, lambda entry: audit.entry_hash(entry, TEST_AUDIT_KEY))
    with world.database.read() as db:
        assert audit.verify_chain(db).ok
        assert audit.find_anchor(db, anchor) is None


@pytest.mark.parametrize(
    ("candidate", "valid"),
    [
        ("0123456789abcdef", True),
        ("0" * 64, True),
        ("0123456789abcde", False),
        ("0" * 65, False),
        ("0123456789ABCDEF", False),
        ("0123456789abcdeg", False),
        ("0123456789abcde%", False),
    ],
)
def test_anchor_syntax(candidate: str, valid: bool) -> None:
    assert audit.is_anchor(candidate) is valid


def test_a_different_key_fails_verification(
    world: World, logged: list[AuditEntry], database_path: Path
) -> None:
    other = Database(f"sqlite:///{database_path}", audit_key=b"another-key" * 3)
    try:
        with other.read() as db:
            report = audit.verify_chain(db)
    finally:
        other.dispose()
    assert (report.ok, report.broken_at, report.checked) == (False, logged[0].id, 0)


def test_no_unkeyed_fallback(world: World, owner: User, database_path: Path) -> None:
    unkeyed = Database(f"sqlite:///{database_path}")
    try:
        with pytest.raises(audit.AuditKeyMissingError, match="audit_key"), unkeyed.write() as db:
            audit.record(db, "test.action", user_id=owner.id)
        with pytest.raises(audit.AuditKeyMissingError), unkeyed.read() as db:
            audit.verify_chain(db)
    finally:
        unkeyed.dispose()
    with world.database.read() as db:
        assert db.scalars(select(AuditEntry)).all() == []


def test_each_session_gets_the_key_and_keeps_its_own_info(world: World, owner: User) -> None:
    """The key reaches every session; what a session adds to ``info`` stays in it."""
    with world.database.write() as db:
        assert audit_key_of(db) == TEST_AUDIT_KEY
        audit.act_as_delegate(db, 7)
        assert audit.record(db, "first", user_id=owner.id).actor == audit.Actor.DELEGATE
    with world.database.write() as db:
        assert audit_key_of(db) == TEST_AUDIT_KEY
        assert audit.record(db, "second", user_id=owner.id).actor == audit.Actor.USER
    with world.database.read() as db:
        assert audit_key_of(db) == TEST_AUDIT_KEY
        assert audit.verify_chain(db).ok


def test_verification_reads_in_batches(world: World, logged: list[AuditEntry]) -> None:
    with world.database.read() as db:
        assert len(list(audit._entries(db, batch=2))) == 4


@pytest.fixture
def cli_database(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Database]:
    monkeypatch.setenv("AGAURA_DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("AGAURA_SECRET_KEY", TEST_SECRET_KEY)
    get_settings.cache_clear()
    database = Database(f"sqlite:///{database_path}", audit_key=TEST_AUDIT_KEY)
    yield database
    database.dispose()
    get_settings.cache_clear()


def test_cli_audit_verify(
    world: World,
    logged: list[AuditEntry],
    cli_database: Database,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["audit", "verify"]) == 0
    assert "4 entries, chain intact" in capsys.readouterr().out
    with cli_database.write() as db:
        db.execute(text("UPDATE audit_entry SET action = 'x' WHERE id = 3"))
    assert cli.main(["audit", "verify"]) == 1
    err = capsys.readouterr().err
    assert "BROKEN at entry 3 (2 entries verified before it)" in err
    assert "AGAURA_SECRET_KEY" in err


def test_cli_audit_verify_with_another_secret_key(
    world: World,
    logged: list[AuditEntry],
    cli_database: Database,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGAURA_SECRET_KEY", "another-secret-key-0123456789abcdef")
    get_settings.cache_clear()
    assert cli.main(["audit", "verify"]) == 1
    err = capsys.readouterr().err
    assert "BROKEN at entry 1 (0 entries verified before it)" in err
    assert "AGAURA_SECRET_KEY is not the key in use" in err


def test_cli_audit_verify_anchor(
    world: World,
    logged: list[AuditEntry],
    cli_database: Database,
    capsys: pytest.CaptureFixture[str],
) -> None:
    anchor = logged[-1].hash[: audit.ANCHOR_LENGTH]
    assert cli.main(["audit", "verify", "--anchor", f" {anchor.upper()} "]) == 0
    assert f"Anchor {anchor} found: entry {logged[-1].id}." in capsys.readouterr().out
    assert cli.main(["audit", "verify", "--anchor", "0" * 16]) == 1
    assert "NOT FOUND" in capsys.readouterr().err
    assert cli.main(["audit", "verify", "--anchor", "3f2a"]) == 1
    assert "Error: The anchor is the hash printed in PDF minutes" in capsys.readouterr().err
    with cli_database.write() as db:  # a broken chain is reported before any anchor
        db.execute(text("DELETE FROM audit_entry WHERE id = 2"))
    assert cli.main(["audit", "verify", "--anchor", anchor]) == 1
    err = capsys.readouterr().err
    assert "BROKEN at entry 3" in err and "NOT FOUND" not in err


def test_cli_db_upgrade(cli_database: Database, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["db", "upgrade"]) == 0
    assert "up to date" in capsys.readouterr().out
