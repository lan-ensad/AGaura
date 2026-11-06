"""Login sessions seen from the browser (FR-03): the absolute lifetime, and a login or a signup
that ends the login session the browser held before."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agaura.config import Settings, SignupMode
from agaura.models import User, UserSession, utcnow
from agaura.services import auth
from agaura.web.deps import SESSION_COOKIE
from agaura.web.public import COOKIE_MAX_AGE
from tests.conftest import World
from tests.web.account.conftest import sign_up
from tests.web.conftest import PASSWORD, log_in, make_account, post


def sessions(world: World) -> list[UserSession]:
    with world.database.read() as db:
        return list(db.scalars(select(UserSession).order_by(UserSession.id)))


def session_count(world: World) -> int:
    with world.database.read() as db:
        return db.scalar(select(func.count()).select_from(UserSession)) or 0


def copy_of(client: TestClient) -> TestClient:
    """Another browser holding a copy of the login cookie (stolen, or left on a computer)."""
    other = TestClient(client.app, follow_redirects=False)
    other.cookies.set(SESSION_COOKIE, client.cookies[SESSION_COOKIE])
    return other


class TestAbsoluteLifetime:
    def test_logged_out_7_days_after_logging_in_even_if_used(
        self, admin: TestClient, world: World
    ) -> None:
        assert admin.get("/account").status_code == 200
        with world.database.write() as db:
            row = db.scalar(select(UserSession))
            assert row is not None
            row.created_at = utcnow() - auth.SESSION_MAX_AGE + timedelta(hours=3)
            row.expires_at = utcnow() + timedelta(hours=1)  # due for renewal
        assert admin.get("/account").status_code == 200
        (row,) = sessions(world)
        assert row.expires_at == row.created_at + auth.SESSION_MAX_AGE  # renewed up to the end
        with world.database.write() as db:
            row = db.scalar(select(UserSession))
            assert row is not None
            row.created_at = utcnow() - auth.SESSION_MAX_AGE - timedelta(minutes=1)
            row.expires_at = utcnow() + timedelta(hours=11)  # used a moment ago
        response = admin.get("/account")
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

    def test_cookie_lasts_as_long_as_the_session_can(self, client: TestClient, owner: User) -> None:
        response = post(client, "/login", {"email": owner.email, "password": PASSWORD})
        assert COOKIE_MAX_AGE == 7 * 24 * 3600
        cookie = next(
            value
            for value in response.headers.get_list("set-cookie")
            if value.startswith(f"{SESSION_COOKIE}=")
        )
        assert f"Max-Age={COOKIE_MAX_AGE}" in cookie


class TestLoginEndsThePreviousSession:
    def test_same_account(self, admin: TestClient, world: World) -> None:
        """A login form opened before (another tab, the back button) and sent again: the first
        session ends, a copy of its cookie no longer works."""
        earlier = copy_of(admin)
        log_in(admin)
        assert session_count(world) == 1
        assert admin.get("/account").status_code == 200
        assert earlier.get("/account").status_code == 303

    def test_another_account(self, admin: TestClient, world: World, owner: User) -> None:
        other = make_account(world, "other@example.org")
        earlier = copy_of(admin)
        log_in(admin, other.email)
        (row,) = sessions(world)
        assert row.user_id == other.id
        assert "other@example.org" in admin.get("/account").text
        assert earlier.get("/account").status_code == 303

    def test_a_failed_login_keeps_it(self, admin: TestClient, world: World) -> None:
        response = post(admin, "/login", {"email": "owner@example.org", "password": "wrong"})
        assert response.status_code == 400
        assert session_count(world) == 1
        assert admin.get("/account").status_code == 200

    def test_other_browsers_keep_theirs(self, admin: TestClient, world: World) -> None:
        elsewhere = TestClient(admin.app, follow_redirects=False)
        log_in(elsewhere)
        log_in(admin)
        assert session_count(world) == 2
        assert elsewhere.get("/account").status_code == 200


class TestSignupEndsThePreviousSession:
    @pytest.fixture
    def settings(self, settings: Settings) -> Settings:
        return settings.model_copy(update={"signup": SignupMode.OPEN})

    def test_signup(self, admin: TestClient, world: World) -> None:
        earlier = copy_of(admin)
        assert sign_up(admin).status_code == 303
        (row,) = sessions(world)
        with world.database.read() as db:
            user = db.get(User, row.user_id)
            assert user is not None and user.email == "new@example.org"
        assert earlier.get("/account").status_code == 303
