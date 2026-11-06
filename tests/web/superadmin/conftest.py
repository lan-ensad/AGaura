"""Fixtures for the super-admin area: the super-admin account and a client logged in as it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agaura.models import User
from agaura.services import accounts
from tests.conftest import World
from tests.web.conftest import PASSWORD, log_in

SUPERADMIN_EMAIL = "root@example.org"


def make_superadmin(world: World, email: str = SUPERADMIN_EMAIL) -> User:
    with world.database.write() as db:
        return accounts.create_user_by_operator(db, email, PASSWORD, superadmin=True)


@pytest.fixture
def superadmin(world: World) -> User:
    return make_superadmin(world)


@pytest.fixture
def root(client: TestClient, superadmin: User) -> TestClient:
    """A client logged in as the super-admin."""
    log_in(client, superadmin.email)
    return client
