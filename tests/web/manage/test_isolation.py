"""Account isolation (FR-07, PLAN P3): every console route, generated from the application's
routes, answers 404 to another account and redirects anonymous visitors to the login page."""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agaura.models import AccessMode, AuditEntry, User
from agaura.services import access
from agaura.web import manage
from tests.conftest import World
from tests.web.conftest import log_in, make_account, post

# Read routes that answer 409 for the ``target`` fixture (a session in preparation whose only
# poll is a draft), because what they read does not exist yet, e.g. the export of a poll that
# has not been closed. Every other read route must answer 200 to the owner.
NOT_READY: dict[str, int] = {
    "/manage/{public_id}/polls/{poll_id}/ballots.csv": 409,
    "/manage/{public_id}/polls/{poll_id}/export.json": 409,
}


def manage_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every route of the console router (included without prefix) under ``/manage/``."""
    routes = []
    for route in manage.router.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/manage/"):
            routes.extend((method, route.path) for method in sorted(route.methods))
    return routes


@pytest.fixture
def target(world: World, owner: User) -> dict[str, str]:
    """A session of ``owner`` with a poll and a code batch, for filling route parameters."""
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    world.codes(owner, public_id, 2)
    poll_id = world.poll(owner, public_id, open_it=False)
    with world.database.read() as db:
        batch_id = access.list_code_batches(db, owner, public_id)[0].id
    return {"public_id": public_id, "poll_id": str(poll_id), "batch_id": str(batch_id)}


def fill(path: str, values: dict[str, str]) -> str:
    return re.sub(r"\{(\w+)\}", lambda match: values[match.group(1)], path)


def audit_count(world: World) -> int:
    with world.database.read() as db:
        return db.scalar(select(func.count()).select_from(AuditEntry)) or 0


def test_routes_are_discovered(app: FastAPI) -> None:
    paths = {path for _, path in manage_routes(app)}
    assert len(paths) >= 15
    assert "/manage/{public_id}/polls/{poll_id}/close" in paths


def test_another_account_gets_404_everywhere(
    app: FastAPI, client: TestClient, world: World, target: dict[str, str]
) -> None:
    make_account(world, "intruder@example.org")
    log_in(client, "intruder@example.org")
    before = audit_count(world)
    for method, path in manage_routes(app):
        url = fill(path, target)
        response = client.get(url) if method == "GET" else post(client, url, {"confirm": "yes"})
        assert response.status_code == 404, f"{method} {url}"
    assert audit_count(world) == before  # nothing happened


def test_anonymous_visitors_go_to_login(
    app: FastAPI, client: TestClient, target: dict[str, str]
) -> None:
    for method, path in manage_routes(app):
        url = fill(path, target)
        response = client.get(url) if method == "GET" else post(client, url)
        assert response.status_code == 303, f"{method} {url}"
        assert response.headers["location"].startswith("/login?next=")


def test_owner_reaches_the_read_routes(
    app: FastAPI, admin: TestClient, target: dict[str, str]
) -> None:
    for method, path in manage_routes(app):
        if method == "GET" and not path.endswith("/events"):
            expected = NOT_READY.get(path, 200)
            assert admin.get(fill(path, target)).status_code == expected, path
