from __future__ import annotations

import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from agaura.config import Settings
from agaura.db import Database
from agaura.models import AccessMode, User
from agaura.security import Keyring
from agaura.services import access, assemblies, ballots, polls
from agaura.services.assemblies import AssemblyInput
from agaura.services.polls import PollInput

TEST_SECRET_KEY = "test-secret-key-0123456789abcdef-0123456789"
# Key of the audit log chain, as the application derives it from ``TEST_SECRET_KEY``.
TEST_AUDIT_KEY = Keyring(TEST_SECRET_KEY).audit_key

RESOLUTION = PollInput(
    title="Approbation des comptes",
    method="single_choice",
    options=["Pour", "Contre"],
    params={"adopt_option": 1},
    blank_label="Abstention",
)


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One migrated database per test run, copied for each test (migrations are slow)."""
    path = tmp_path_factory.mktemp("template") / "template.db"
    database = Database(f"sqlite:///{path}", audit_key=TEST_AUDIT_KEY)
    database.upgrade()
    database.dispose()  # checkpoints the WAL into the main file
    return path


@pytest.fixture
def database_path(tmp_path: Path, migrated_template: Path) -> Path:
    path = tmp_path / "agaura.db"
    shutil.copyfile(migrated_template, path)
    return path


@pytest.fixture
def database(database_path: Path) -> Iterator[Database]:
    database = Database(f"sqlite:///{database_path}", audit_key=TEST_AUDIT_KEY)
    yield database
    database.dispose()


@pytest.fixture
def keyring() -> Keyring:
    return Keyring(TEST_SECRET_KEY)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", secret_key=TEST_SECRET_KEY, _env_file=None)  # type: ignore[call-arg]


class World:
    """Shortcuts to build sessions, voters and polls; each call is its own transaction."""

    def __init__(self, database: Database, keyring: Keyring) -> None:
        self.database = database
        self.keyring = keyring

    def user(self, email: str = "owner@example.org", **quota_overrides: int) -> User:
        # Direct insert: argon2 hashing is slow and irrelevant here.
        with self.database.write() as db:
            user = User(email=email, password_hash="-", quota_overrides=quota_overrides)
            db.add(user)
            db.flush()
            return user

    def assembly(
        self,
        owner: User,
        *,
        mode: AccessMode = AccessMode.PASSWORD,
        in_session: bool = True,
        **fields: Any,
    ) -> str:
        fields.setdefault("title", "Assemblée générale")
        with self.database.write() as db:
            assembly = assemblies.create_assembly(
                db, self.keyring, owner, AssemblyInput(**fields), access_mode=mode
            )
            if in_session:
                assemblies.open_assembly(db, owner, assembly.public_id)
            return assembly.public_id

    def password(self, owner: User, public_id: str) -> str:
        with self.database.read() as db:
            return access.session_password(db, self.keyring, owner, public_id)

    def join(self, public_id: str, secret: str) -> str:
        with self.database.write() as db:
            return access.join(db, self.keyring, public_id, secret).token

    def voters(self, owner: User, public_id: str, count: int) -> list[str]:
        password = self.password(owner, public_id)
        return [self.join(public_id, password) for _ in range(count)]

    def codes(self, owner: User, public_id: str, count: int) -> list[str]:
        with self.database.write() as db:
            return access.create_code_batch(db, self.keyring, owner, public_id, count).codes

    def poll(
        self, owner: User, public_id: str, data: PollInput = RESOLUTION, *, open_it: bool = True
    ) -> int:
        with self.database.write() as db:
            poll = polls.create_poll(db, owner, public_id, data)
            if open_it:
                polls.open_poll(db, owner, poll.id)
            return poll.id

    def vote(
        self, public_id: str, token: str, poll_id: int, content: Mapping[str, Any] | None
    ) -> None:
        with self.database.write() as db:
            ballots.cast_vote(db, public_id, token, poll_id, content)


@pytest.fixture
def world(database: Database, keyring: Keyring) -> World:
    return World(database, keyring)


@pytest.fixture
def owner(world: World) -> User:
    return world.user()
