"""Closed signup (default, ``AGAURA_SIGNUP=closed``): accounts come from invitations only."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agaura.config import Settings, SignupMode
from agaura.main import create_app
from agaura.models import User
from tests.conftest import World
from tests.web.conftest import PASSWORD, post


def test_closed_is_the_default() -> None:
    assert Settings(_env_file=None).signup is SignupMode.CLOSED  # type: ignore[call-arg]


def test_signup_page_explains_invitations(client: TestClient) -> None:
    page = client.get("/signup")
    assert page.status_code == 200
    assert "Les comptes sont créés sur invitation" in page.text
    assert '<a href="mailto:contact@agaura.org">contact@agaura.org</a>' in page.text
    assert "<form" not in page.text.split('<main class="page"')[1]  # no form in the content
    assert "pow.js" not in page.text


def test_signup_form_is_refused(client: TestClient, world: World) -> None:
    data = {"email": "new@example.org", "password": PASSWORD, "password_confirm": PASSWORD}
    response = post(client, "/signup", data)
    assert response.status_code == 403
    assert "Les comptes sont créés sur invitation" in response.text
    with world.database.read() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0


def test_anti_bot_challenge_still_served_for_the_login_form(client: TestClient) -> None:
    response = client.get("/altcha/challenge")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"


def test_home_and_login_no_longer_offer_signup(client: TestClient) -> None:
    for path in ("/", "/login"):
        page = client.get(path).text
        assert 'href="/signup"' not in page, path
    contact = 'Pour obtenir un compte, écrivez à <a href="mailto:contact@agaura.org">'
    for path in ("/", "/login"):  # how to ask for an account, on the login page too
        assert contact in client.get(path).text, path


def test_open_mode_offers_signup(settings: Settings, database_path: Path) -> None:
    app: FastAPI = create_app(
        settings.model_copy(
            update={"signup": SignupMode.OPEN, "database_url": f"sqlite:///{database_path}"}
        )
    )
    with TestClient(app, follow_redirects=False) as client:
        for path in ("/", "/login"):
            page = client.get(path).text
            assert 'href="/signup"' in page and "Pour obtenir un compte" not in page, path
        assert client.get("/altcha/challenge").status_code == 200
        assert "data-challenge-url" in client.get("/signup").text
