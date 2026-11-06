"""Invitation link: the invited person chooses a password, which confirms the address and logs
in; pending accounts cannot log in nor get a reset link."""

from __future__ import annotations

import html
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select

from agaura.models import EmailToken, User
from agaura.services import accounts
from agaura.web.deps import SESSION_COOKIE
from tests.conftest import World
from tests.web.account.conftest import actions, emails_to
from tests.web.conftest import PASSWORD, log_in, make_account, post

NEW_PASSWORD = "my own long passphrase"


@pytest.fixture
def token(world: World) -> str:
    """The invitation link token of a new pending account ``new@example.org``."""
    with world.database.write() as db:
        root = accounts.create_user_by_operator(db, "root@example.org", PASSWORD, superadmin=True)
        return accounts.invite(db, root, "new@example.org", display_name="Camille")[1]


def pending(world: World) -> User:
    with world.database.read() as db:
        user = db.scalar(select(User).where(User.email == "new@example.org"))
    assert user is not None
    return user


def accept(client: TestClient, token: str, password: str = NEW_PASSWORD, **fields: str) -> Response:
    data = {"password": password, "password_confirm": password, **fields}
    return post(client, f"/invite/{token}", data)


class TestForm:
    def test_shows_the_address(self, client: TestClient, token: str) -> None:
        page = client.get(f"/invite/{token}")
        assert page.status_code == 200
        assert "Bienvenue sur AGaura" in page.text and 'value="new@example.org"' in page.text
        assert 'autocomplete="username"' in page.text and "readonly" in page.text
        assert f'action="/invite/{token}"' in page.text
        assert page.headers["referrer-policy"] == "no-referrer"

    def test_accept_logs_in_on_an_empty_dashboard(
        self, client: TestClient, world: World, token: str
    ) -> None:
        response = accept(client, token)
        assert response.status_code == 303 and response.headers["location"] == "/dashboard"
        assert response.headers["referrer-policy"] == "no-referrer"
        dashboard = client.get("/dashboard")
        assert dashboard.status_code == 200
        assert "Bienvenue sur AGaura." in html.unescape(dashboard.text)
        assert "Aucune session pour l'instant." in html.unescape(dashboard.text)
        assert "Confirmez votre adresse e-mail" not in dashboard.text
        user = pending(world)
        assert user.email_verified_at is not None and user.password_hash is not None
        assert actions(world, user.id) == ["account.invited", "account.invitation_accepted"]
        # The new password works on the login page; the link does not work twice.
        post(client, "/logout")
        log_in_response = post(
            client, "/login", {"email": "new@example.org", "password": NEW_PASSWORD}
        )
        assert log_in_response.headers["location"] == "/dashboard"
        again = client.get(f"/invite/{token}")
        assert again.status_code == 400 and "déjà été utilisé" in again.text

    def test_replaces_whoever_was_logged_in(
        self, client: TestClient, world: World, token: str
    ) -> None:
        make_account(world, "someone@example.org")
        log_in(client, "someone@example.org")
        before = client.cookies[SESSION_COOKIE]
        accept(client, token)
        assert client.cookies[SESSION_COOKIE] != before
        assert "new@example.org" in client.get("/account").text

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"password": "short", "password_confirm": "short"}, "trop court"),
            ({"password_confirm": "something else entirely"}, "ne correspondent pas"),
        ],
    )
    def test_invalid_password(
        self, client: TestClient, world: World, token: str, fields: dict[str, str], message: str
    ) -> None:
        response = accept(client, token, **fields)
        assert response.status_code == 400 and message in response.text
        assert 'value="new@example.org"' in response.text
        assert pending(world).password_hash is None
        assert client.get(f"/invite/{token}").status_code == 200  # the link still works


class TestInvalidLinks:
    def test_unknown(self, client: TestClient) -> None:
        for response in (client.get("/invite/" + "x" * 43), accept(client, "x" * 43)):
            assert response.status_code == 400 and "Lien non valable" in response.text
            assert "nouvelle invitation" in response.text
            assert 'href="/login"' in response.text
        assert client.get("/invite/" + "x" * 300).status_code in {400, 404}

    def test_expired(self, client: TestClient, world: World, token: str) -> None:
        with world.database.write() as db:
            for row in db.scalars(select(EmailToken)):
                row.expires_at -= timedelta(days=8)
        for response in (client.get(f"/invite/{token}"), accept(client, token)):
            assert response.status_code == 400 and "Ce lien a expiré." in response.text
        assert pending(world).password_hash is None

    def test_rate_limited_per_ip(self, client: TestClient, token: str) -> None:
        for _ in range(30):
            mismatch = accept(client, token, password_confirm="different password!")
            assert mismatch.status_code == 400
        blocked = accept(client, token)
        assert blocked.status_code == 429 and "Trop de tentatives" in blocked.text


class TestPendingAccount:
    def test_cannot_log_in(self, client: TestClient, world: World, token: str) -> None:
        for password in ("", NEW_PASSWORD):
            response = post(client, "/login", {"email": "new@example.org", "password": password})
            assert response.status_code == 400
            assert "Adresse e-mail ou mot de passe incorrect." in response.text

    def test_reset_sends_nothing_with_the_generic_answer(
        self, client: TestClient, world: World, token: str
    ) -> None:
        response = post(client, "/reset", {"email": "new@example.org"})
        unknown = post(client, "/reset", {"email": "nobody@example.org"})
        assert response.status_code == unknown.status_code == 200
        assert response.text == unknown.text
        assert emails_to(world, "new@example.org") == []  # the invitation was queued elsewhere
