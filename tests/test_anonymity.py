"""Ballot secrecy (SPEC §9.2): structural guarantees that must never regress."""

import datetime as dt
import json
import random
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import Date, DateTime, select, text

from agaura import cli, tasks
from agaura.db import Database
from agaura.models import AccessMode, AuditEntry, Ballot, Base, User, utcnow
from agaura.services import accounts, assemblies, polls
from agaura.services.polls import PollInput
from tests.conftest import World

SECRET_TABLES = ("ballot", "participation", "voter_credential")
TIME_LIKE_NAME = re.compile(r"(^|_)(at|time|date|timestamp|created|updated|seen)($|_)")


@pytest.mark.parametrize("table", SECRET_TABLES)
def test_no_timestamp_column(table: str) -> None:
    for column in Base.metadata.tables[table].columns:
        assert not isinstance(column.type, DateTime | Date), column.name
        assert not TIME_LIKE_NAME.search(column.name), column.name
        assert column.server_default is None, column.name


def test_ballot_has_no_link_to_a_credential_or_participation() -> None:
    ballot = Base.metadata.tables["ballot"]
    assert set(ballot.columns.keys()) == {"id", "poll_id", "is_blank", "content"}
    assert {fk.column.table.name for fk in ballot.foreign_keys} == {"poll"}


def test_participation_records_only_who_voted_on_which_poll() -> None:
    participation = Base.metadata.tables["participation"]
    assert set(participation.columns.keys()) == {"poll_id", "credential_id"}
    referenced = {fk.column.table.name for fk in participation.foreign_keys}
    assert referenced == {"poll", "voter_credential"}


def test_nothing_else_references_ballots_or_participations() -> None:
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            assert fk.column.table.name not in {"ballot", "participation"}, table.name


def test_credentials_do_not_identify_people() -> None:
    columns = set(Base.metadata.tables["voter_credential"].columns.keys())
    assert columns == {
        "id",
        "assembly_id",
        "kind",
        "token_hash",
        "code_hmac",
        "batch_id",
        "activated",
    }


@pytest.mark.parametrize("table", ["ballot", "participation"])
def test_tables_are_without_rowid(database: Database, table: str) -> None:
    with database.read() as db:
        ddl = db.scalar(text("SELECT sql FROM sqlite_master WHERE name = :name"), {"name": table})
    assert ddl is not None and "WITHOUT ROWID" in ddl


def test_storage_order_does_not_reveal_arrival_order(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id)
    tokens = world.voters(owner, public_id, 30)
    arrival = []
    for token in tokens:
        world.vote(public_id, token, poll_id, {"choice": 1})
        with world.database.read() as db:
            known = set(arrival)
            arrival.extend(i for i in db.scalars(select(Ballot.id)) if i not in known)
    with world.database.read() as db:
        stored = list(db.scalars(text("SELECT id FROM ballot")))
    assert all(re.fullmatch(r"[0-9a-f]{32}", ballot_id) for ballot_id in stored)
    assert stored == sorted(stored)  # physical order is the random id order
    assert stored != arrival  # 1 chance in 30! of a false failure


# --- Physical layout of the database file (SPEC §9.2) -----------------------------------------
#
# SELECT order is not enough: inside each B-tree page, SQLite allocates cell content downward in
# insertion order, and the WAL holds one commit per vote, so the raw files pair each ballot with
# its participation. Closing a poll rebuilds the file in key order and empties the WAL.

INDEX_INTERIOR_PAGE, INDEX_LEAF_PAGE = 0x02, 0x0A
# B-trees written once per vote: WITHOUT ROWID tables and their indexes are index B-trees.
VOTE_TREES = {"ballot", "ix_ballot_poll_id", "participation", "ix_participation_credential_id"}


def leaf_pages(data: bytes, number: int) -> list[list[int]]:
    """Cell content offsets of each leaf page of an index B-tree, leaves and cells in key order.

    Content offsets record the order in which cells were written into each page.
    """
    page_size = int.from_bytes(data[16:18], "big")
    page_size = 65536 if page_size == 1 else page_size
    page = data[(number - 1) * page_size : number * page_size]
    base = 100 if number == 1 else 0  # page 1 starts with the file header
    kind = page[base]
    count = int.from_bytes(page[base + 3 : base + 5], "big")
    pointers = base + (12 if kind == INDEX_INTERIOR_PAGE else 8)
    offsets = [
        int.from_bytes(page[pointers + 2 * k : pointers + 2 * k + 2], "big") for k in range(count)
    ]
    if kind == INDEX_LEAF_PAGE:
        return [offsets]
    assert kind == INDEX_INTERIOR_PAGE, kind
    children = [int.from_bytes(page[offset : offset + 4], "big") for offset in offsets]
    children.append(int.from_bytes(page[base + 8 : base + 12], "big"))  # right-most child
    return [leaf for child in children for leaf in leaf_pages(data, child)]


def vote_trees(path: Path) -> dict[str, list[list[int]]]:
    connection = sqlite3.connect(path)
    try:
        roots = dict(
            connection.execute(
                "SELECT name, rootpage FROM sqlite_master "
                "WHERE tbl_name IN ('ballot', 'participation') AND rootpage > 0"
            ).fetchall()
        )
    finally:
        connection.close()
    assert set(roots) == VOTE_TREES
    data = path.read_bytes()
    return {name: leaf_pages(data, root) for name, root in roots.items()}


def key_order_layout(path: Path, copy: Path) -> dict[str, list[list[int]]]:
    """Layout of a fresh rebuild, which reads every row in key order (``VACUUM INTO``).

    Strictly decreasing offsets are not the reference: bulk loading may put the last cell of a
    full page back into a freed slot, deterministically.
    """
    copy.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("VACUUM INTO ?", (str(copy),))
    finally:
        connection.close()
    return vote_trees(copy)


def checkpoint(database: Database) -> None:
    raw = database.engine.raw_connection()
    try:
        raw.cursor().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        raw.close()


def test_closing_a_poll_erases_arrival_order_from_the_files(
    world: World, owner: User, database_path: Path, tmp_path: Path
) -> None:
    public_id = world.assembly(owner)
    tokens = world.voters(owner, public_id, 100)  # ballots span several leaf pages
    poll_id = world.poll(owner, public_id)
    random.Random(7).shuffle(tokens)  # noqa: S311 (voters do not vote in the order they joined)
    for number, token in enumerate(tokens):
        world.vote(public_id, token, poll_id, {"choice": 1 + number % 2})
    reference = tmp_path / "reference.db"

    # While the poll is open, the files keep the arrival order (SPEC §9.1, known limits): the
    # comparison below does see it.
    checkpoint(world.database)
    open_layout = vote_trees(database_path)
    expected = key_order_layout(database_path, reference)
    for name in VOTE_TREES:
        assert open_layout[name] != expected[name], name

    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)

    wal = database_path.with_name(database_path.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    closed_layout = vote_trees(database_path)
    assert len(closed_layout["ballot"]) > 1
    assert closed_layout == key_order_layout(database_path, reference)
    with world.database.read() as db:
        assert len(db.scalars(select(Ballot.id)).all()) == 100


def test_storage_is_rebuilt_whenever_voting_ends_or_ballots_go(
    world: World, owner: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Database] = []
    monkeypatch.setattr(Database, "compact", lambda self: calls.append(self) or True)

    def compactions(action: Callable[[], object]) -> int:
        before = len(calls)
        action()
        return len(calls) - before

    def write(step: Callable[..., object], *args: object, **kwargs: object) -> Callable[[], None]:
        def run() -> None:
            with world.database.write() as db:
                step(db, *args, **kwargs)

        return run

    public_id = world.assembly(owner)
    token = world.voters(owner, public_id, 1)[0]
    closed, cancelled = world.poll(owner, public_id), world.poll(owner, public_id)
    assert compactions(lambda: world.vote(public_id, token, closed, None)) == 0
    assert compactions(write(polls.close_poll, owner, closed)) == 1
    assert compactions(write(polls.cancel_poll, owner, cancelled, "Erreur")) == 1

    timed = world.poll(owner, public_id, open_it=False)
    assert compactions(lambda: tasks.close_expired_polls(world.database)) == 0  # every 5 s
    past = utcnow() - dt.timedelta(minutes=5)
    assert compactions(write(polls.open_poll, owner, timed, minutes=1, now=past)) == 0
    assert compactions(lambda: tasks.close_expired_polls(world.database)) == 1

    assert compactions(write(assemblies.close_assembly, owner, public_id)) == 1
    assert compactions(write(assemblies.delete_assembly, owner, public_id)) == 1
    forced = world.assembly(owner)
    assert compactions(write(cli._delete_session, forced, force=True)) == 1
    world.assembly(owner, in_session=False)
    assert compactions(write(accounts.delete_account_by_operator, owner.email)) == 1


def test_audit_log_never_contains_secrets_or_ballot_content(world: World, owner: User) -> None:
    password_id = world.assembly(owner)
    password = world.password(owner, password_id)
    tokens = world.voters(owner, password_id, 3)
    poll_id = world.poll(
        owner, password_id, PollInput("Choix", "single_choice", ["Pomme", "Poire"])
    )
    for token in tokens:
        world.vote(password_id, token, poll_id, {"choice": 2})

    codes_id = world.assembly(owner, mode=AccessMode.CODES)
    codes = world.codes(owner, codes_id, 5)
    code_tokens = [world.join(codes_id, code) for code in codes[:2]]

    with world.database.read() as db:
        logged = json.dumps([entry.details for entry in db.scalars(select(AuditEntry))])
    forbidden = [
        password,
        *codes,
        *(code.replace("-", "") for code in codes),
        *tokens,
        *code_tokens,
    ]
    for secret in forbidden:
        assert secret not in logged
    assert '"choice"' not in logged  # ballot content key


# One ballot of each method (ballot content as stored, docs/EXPORTS.md), for the checks below.
METHOD_BALLOTS = {
    "single_choice": (PollInput("Choix", "single_choice", ["Pomme", "Poire"]), {"choice": 2}),
    "approval": (
        PollInput("Conseil", "approval", ["Alice", "Bruno", "Chloé"]),
        {"approved": [1, 3]},
    ),
    "majority_judgment": (
        PollInput("Projets", "majority_judgment", ["Jardin", "Fresque"]),
        {"grades": {"1": 4, "2": 0}},
    ),
    "condorcet": (
        PollInput("Lettre", "condorcet", ["Gazette", "Écho", "Lien"]),
        {"ranks": {"1": 2, "2": 1, "3": 3}},
    ),
}
CONTENT_KEYS = ("choice", "approved", "grades", "ranks")


@pytest.mark.parametrize("trial", [False, True], ids=["poll", "trial"])
@pytest.mark.parametrize("method", METHOD_BALLOTS)
def test_audit_log_never_holds_a_ballot_of_any_method(
    world: World, owner: User, method: str, trial: bool
) -> None:
    """A ballot, a blank vote and the closing, publication and display of the result: the
    session log keeps counts and digests only, never what a ballot holds."""
    data, content = METHOD_BALLOTS[method]
    public_id = world.assembly(owner)
    tokens = world.voters(owner, public_id, 2)
    with world.database.write() as db:
        if trial:
            poll = polls.create_trial_poll(db, owner, public_id, data)
        else:
            poll = polls.create_poll(db, owner, public_id, data)
        polls.open_poll(db, owner, poll.id)
        poll_id = poll.id
    world.vote(public_id, tokens[0], poll_id, content)
    world.vote(public_id, tokens[1], poll_id, None)  # blank
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
        polls.publish_result(db, owner, poll_id, voters=True)
    with world.database.read() as db:
        logged = json.dumps([entry.details for entry in db.scalars(select(AuditEntry))])
    for key in CONTENT_KEYS:
        assert f'"{key}"' not in logged, key
    assert json.dumps(content) not in logged
    assert world.password(owner, public_id) not in logged
    assert not any(token in logged for token in tokens)
