"""Who reaches what: the super-admin never reaches the dashboard or the console (generated from
every console route), account holders never reach ``/superadmin``."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agaura.models import AccessMode, Assembly, AuditEntry, User
from agaura.services import access
from agaura.web import superadmin
from tests.conftest import World
from tests.web.conftest import PASSWORD, log_in, make_account, post
from tests.web.manage.test_isolation import fill, manage_routes

REFUSAL = "Non disponible pour le super-admin"


def superadmin_routes() -> list[tuple[str, str]]:
    return [
        (method, route.path)
        for route in superadmin.router.routes
        if isinstance(route, APIRoute)
        for method in sorted(route.methods)
    ]


@pytest.fixture
def target(world: World, owner: User) -> dict[str, str]:
    """A session of an account holder with a poll and a code batch, as in the isolation test."""
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    world.codes(owner, public_id, 2)
    poll_id = world.poll(owner, public_id, open_it=False)
    with world.database.read() as db:
        batch_id = access.list_code_batches(db, owner, public_id)[0].id
    return {"public_id": public_id, "poll_id": str(poll_id), "batch_id": str(batch_id)}


def audit_count(world: World) -> int:
    with world.database.read() as db:
        return db.scalar(select(func.count()).select_from(AuditEntry)) or 0


def test_routes_are_discovered() -> None:
    paths = {path for _, path in superadmin_routes()}
    assert paths == {
        "/superadmin",
        "/superadmin/invite",
        "/superadmin/accounts/{account_id}/resend",
        "/superadmin/accounts/{account_id}/delete",
    }


class TestSuperAdminIsRefusedEverySessionPage:
    def test_dashboard(self, root: TestClient, world: World) -> None:
        before = audit_count(world)
        for response in (root.get("/dashboard"), post(root, "/dashboard", {"title": "AG"})):
            assert response.status_code == 403 and REFUSAL in response.text
            assert 'href="/superadmin"' in response.text
        with world.database.read() as db:
            assert db.scalar(select(func.count()).select_from(Assembly)) == 0
        assert audit_count(world) == before

    def test_every_console_route(
        self, app: FastAPI, root: TestClient, world: World, target: dict[str, str]
    ) -> None:
        routes = manage_routes(app)
        assert len(routes) >= 15
        before = audit_count(world)
        for method, path in routes:
            url = fill(path, target)
            response = root.get(url) if method == "GET" else post(root, url, {"confirm": "yes"})
            assert response.status_code == 403, f"{method} {url}"
            assert REFUSAL in response.text, f"{method} {url}"
            assert "Assemblée générale" not in response.text  # nothing of the session
        assert audit_count(world) == before  # nothing happened

    def test_same_answer_for_a_session_that_does_not_exist(self, root: TestClient) -> None:
        response = root.get("/manage/ZZZZZ9")
        assert response.status_code == 403 and REFUSAL in response.text


class TestAccountHoldersNeverSeeTheAccountsPage:
    def test_not_found(self, admin: TestClient, superadmin: User, world: World) -> None:
        before = audit_count(world)
        for method, path in superadmin_routes():
            url = fill(path, {"account_id": str(superadmin.id)})
            data = {"email": "x@example.org", "confirm_email": superadmin.email}
            response = admin.get(url) if method == "GET" else post(admin, url, data)
            assert response.status_code == 404, f"{method} {url}"
        assert audit_count(world) == before
        assert 'href="/superadmin"' not in admin.get("/dashboard").text

    def test_anonymous_visitors_go_to_login(self, client: TestClient) -> None:
        for method, path in superadmin_routes():
            url = fill(path, {"account_id": "1"})
            response = client.get(url) if method == "GET" else post(client, url)
            assert response.status_code == 303, f"{method} {url}"
            assert response.headers["location"].startswith("/login?next=")

    def test_csrf_is_required(self, root: TestClient, world: World) -> None:
        response = root.post("/superadmin/invite", data={"email": "x@example.org"})
        assert response.status_code == 403
        with world.database.read() as db:
            assert db.scalar(select(User).where(User.email == "x@example.org")) is None


class TestLanding:
    def test_login_leads_to_the_accounts_page(self, client: TestClient, superadmin: User) -> None:
        response = post(client, "/login", {"email": superadmin.email, "password": PASSWORD})
        assert response.headers["location"] == "/superadmin"
        assert client.get("/login").headers["location"] == "/superadmin"
        assert client.get("/signup").headers["location"] == "/superadmin"
        home = client.get("/").text
        assert 'href="/superadmin"' in home and 'href="/dashboard"' not in home

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            ("/dashboard", "/superadmin"),
            ("/manage/ABC123", "/superadmin"),
            ("/accounts", "/superadmin"),
            ("/account", "/account"),
            ("/superadmin?x=1", "/superadmin?x=1"),
            ("//evil.example", "/superadmin"),
        ],
    )
    def test_next_is_kept_only_for_its_pages(
        self, client: TestClient, superadmin: User, target: str, expected: str
    ) -> None:
        data = {"email": superadmin.email, "password": PASSWORD, "next": target}
        assert post(client, "/login", data).headers["location"] == expected

    def test_account_holders_still_land_on_the_dashboard(
        self, client: TestClient, world: World
    ) -> None:
        make_account(world, "holder@example.org")
        log_in(client, "holder@example.org")
        assert client.get("/login").headers["location"] == "/dashboard"


class TestOwnAccountPage:
    def test_password_and_logout_everywhere_but_no_deletion(
        self, root: TestClient, superadmin: User, world: World
    ) -> None:
        page = root.get("/account")
        assert page.status_code == 200
        assert 'action="/account/password"' in page.text
        assert 'action="/account/logout-everywhere"' in page.text
        assert 'action="/account/delete"' not in page.text
        assert "Compte super-admin" in page.text
        refused = post(root, "/account/delete", {"delete_password": PASSWORD})
        assert refused.status_code == 400 and "super-admin ne peut pas" in refused.text
        with world.database.read() as db:
            assert db.get(User, superadmin.id) is not None

    def test_change_password(self, root: TestClient) -> None:
        new = "another long passphrase"
        data = {"current_password": PASSWORD, "new_password": new, "new_password_confirm": new}
        assert post(root, "/account/password", data).headers["location"] == "/account"

    def test_navigation(self, root: TestClient) -> None:
        page = root.get("/superadmin").text
        assert 'href="/superadmin">Comptes<' in page and 'href="/account">Mon compte<' in page
        assert 'href="/dashboard"' not in page
