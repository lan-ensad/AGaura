"""Fixtures for the web layer: an application on a fresh database, clients, accounts.

``world`` here shares the application's database and keyring, so data built with it is what
the pages show. ``post`` sends a form with the CSRF token, like a browser would.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from agaura.config import Settings
from agaura.main import create_app
from agaura.models import User
from agaura.services import accounts
from agaura.web.security import CSRF_COOKIE
from tests.conftest import World

PASSWORD = "correct horse battery"


@pytest.fixture
def app(settings: Settings, database_path: Path) -> FastAPI:
    return create_app(settings.model_copy(update={"database_url": f"sqlite:///{database_path}"}))


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, follow_redirects=False) as client:
        yield client


@pytest.fixture
def world(app: FastAPI) -> World:
    return World(app.state.database, app.state.keyring)


def make_account(world: World, email: str = "owner@example.org", *, verified: bool = True) -> User:
    with world.database.write() as db:
        return accounts.create_user(db, email, PASSWORD, verified=verified)


@pytest.fixture
def owner(world: World) -> User:
    return make_account(world)


def csrf_token(client: TestClient) -> str:
    if CSRF_COOKIE not in client.cookies:
        client.get("/health")
        client.get("/")
    return client.cookies[CSRF_COOKIE]


def post(
    client: TestClient, url: str, data: dict[str, Any] | None = None, **kwargs: Any
) -> Response:
    """Submit a form like a browser: with the CSRF token of the client's cookie."""
    return client.post(url, data={"csrf_token": csrf_token(client), **(data or {})}, **kwargs)


def log_in(client: TestClient, email: str = "owner@example.org") -> None:
    response = post(client, "/login", {"email": email, "password": PASSWORD})
    assert response.status_code == 303, response.text


@pytest.fixture
def admin(client: TestClient, owner: User) -> TestClient:
    """A client logged in as ``owner``."""
    log_in(client, owner.email)
    return client
