"""Cookie names and attributes (SPEC §9.3): in production, ``__Host-`` for the cookies of the
whole site and ``__Secure-`` for the ones scoped to a session, read under those names only; in
development over plain HTTP, the plain names."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from starlette.responses import Response as StarletteResponse

from agaura.config import Environment, Settings
from agaura.main import create_app
from agaura.models import User
from agaura.services import accounts, delegates
from agaura.web.security import (
    CSRF_COOKIE,
    DELEGATE_COOKIE,
    FLASH_COOKIE,
    SESSION_COOKIE,
    VOTER_COOKIE,
    cookie_name,
    set_cookie,
)
from tests.conftest import World
from tests.web.conftest import PASSWORD

HOST = {name: f"__Host-{name}" for name in (SESSION_COOKIE, CSRF_COOKIE, FLASH_COOKIE)}
SECURE = {name: f"__Secure-{name}" for name in (VOTER_COOKIE, DELEGATE_COOKIE)}


@pytest.fixture
def production_app(settings: Settings, database_path: Path) -> FastAPI:
    production = settings.model_copy(
        update={
            "environment": Environment.PRODUCTION,
            "database_url": f"sqlite:///{database_path}",
        }
    )
    return create_app(production)


@pytest.fixture
def browser(production_app: FastAPI) -> Iterator[TestClient]:
    """Over HTTPS, so that the client keeps and sends back ``Secure`` cookies."""
    with TestClient(production_app, base_url="https://testserver", follow_redirects=False) as c:
        yield c


@pytest.fixture
def production_world(production_app: FastAPI) -> World:
    return World(production_app.state.database, production_app.state.keyring)


def set_cookies(response: Response) -> dict[str, str]:
    """``Set-Cookie`` headers of a response, by cookie name."""
    return {header.split("=", 1)[0]: header for header in response.headers.get_list("set-cookie")}


def attributes(header: str) -> set[str]:
    return {part.strip().lower() for part in header.split(";")[1:]}


def send(browser: TestClient, url: str, data: dict[str, str]) -> Response:
    token = browser.cookies[HOST[CSRF_COOKIE]]
    return browser.post(url, data={"csrf_token": token, **data})


def make_owner(world: World) -> User:
    with world.database.write() as db:
        return accounts.create_user(db, "owner@example.org", PASSWORD, verified=True)


class TestProduction:
    def test_site_cookies_take_the_host_prefix(
        self, browser: TestClient, production_world: World
    ) -> None:
        make_owner(production_world)
        csrf = set_cookies(browser.get("/"))[HOST[CSRF_COOKIE]]
        assert {"path=/", "httponly", "samesite=lax", "secure"} <= attributes(csrf)
        assert not any(attribute.startswith("domain") for attribute in attributes(csrf))

        response = send(browser, "/login", {"email": "owner@example.org", "password": PASSWORD})
        assert response.status_code == 303
        cookies = set_cookies(response)
        assert set(cookies) == {HOST[SESSION_COOKIE], HOST[FLASH_COOKIE]}
        for header in cookies.values():
            assert {"path=/", "httponly", "samesite=lax", "secure"} <= attributes(header)
            assert not any(attribute.startswith("domain") for attribute in attributes(header))

        # Read back under the same names: logged in, the notice shown once and removed.
        page = browser.get("/dashboard")
        assert page.status_code == 200 and "Vous êtes connecté·e." in page.text
        removed = set_cookies(page)[HOST[FLASH_COOKIE]]
        assert "max-age=0" in attributes(removed) and "secure" in attributes(removed)

        response = send(browser, "/logout", {})
        removed = set_cookies(response)[HOST[SESSION_COOKIE]]
        assert {"path=/", "secure", "max-age=0"} <= attributes(removed)
        assert browser.get("/dashboard").status_code == 303

    def test_cookies_without_the_prefix_are_ignored(
        self, browser: TestClient, production_world: World, production_app: FastAPI
    ) -> None:
        """A cookie set by a neighbour host or over plain HTTP cannot carry the ``__Host-``
        prefix: a login cookie or a CSRF cookie under the plain name counts for nothing."""
        make_owner(production_world)
        browser.get("/")
        login = {"email": "owner@example.org", "password": PASSWORD}
        assert send(browser, "/login", login).status_code == 303
        token = browser.cookies[HOST[SESSION_COOKIE]]
        planted = TestClient(production_app, base_url="https://testserver", follow_redirects=False)
        planted.cookies.set(SESSION_COOKIE, token)
        assert planted.get("/dashboard").status_code == 303
        planted.cookies.set(CSRF_COOKIE, "x" * 43)
        refused = planted.post("/logout", data={"csrf_token": "x" * 43})
        assert refused.status_code == 403

    def test_session_cookies_take_the_secure_prefix(
        self, browser: TestClient, production_world: World, production_app: FastAPI
    ) -> None:
        owner = make_owner(production_world)
        public_id = production_world.assembly(owner)
        browser.get("/")

        secret = production_world.password(owner, public_id)
        response = send(browser, f"/{public_id}/join", {"secret": secret})
        assert response.status_code == 303
        voter = set_cookies(response)[SECURE[VOTER_COOKIE]]
        assert {f"path=/{public_id.lower()}", "httponly", "samesite=lax", "secure"} <= attributes(
            voter
        )
        live = browser.get(f"/{public_id}")
        assert live.status_code == 200 and "data-autojoin" not in live.text
        assert browser.cookies.get(SECURE[VOTER_COOKIE])

        with production_world.database.write() as db:
            _, key = delegates.create_delegate(
                db, production_world.keyring, owner, public_id, "Claire", ["timer"]
            )
        response = send(browser, f"/manage/{public_id}/delegate", {"key": key})
        assert response.status_code == 303
        delegate = set_cookies(response)[SECURE[DELEGATE_COOKIE]]
        assert {
            f"path=/manage/{public_id.lower()}",
            "httponly",
            "samesite=lax",
            "secure",
        } <= attributes(delegate)
        assert browser.get(f"/manage/{public_id}").status_code == 200


def test_development_keeps_the_plain_names(client: TestClient, owner: User) -> None:
    csrf = set_cookies(client.get("/"))
    assert set(csrf) == {CSRF_COOKIE} and "secure" not in attributes(csrf[CSRF_COOKIE])
    response = client.post(
        "/login",
        data={
            "csrf_token": client.cookies[CSRF_COOKIE],
            "email": owner.email,
            "password": PASSWORD,
        },
    )
    cookies = set_cookies(response)
    assert set(cookies) == {SESSION_COOKIE, FLASH_COOKIE}
    assert all("secure" not in attributes(header) for header in cookies.values())


def test_cookie_names() -> None:
    assert [cookie_name(name, secure=False) for name in (*HOST, *SECURE)] == [*HOST, *SECURE]
    assert {name: cookie_name(name, secure=True) for name in HOST} == HOST
    assert {name: cookie_name(name, secure=True) for name in SECURE} == SECURE


def test_a_cookie_on_the_wrong_path_is_refused(app: FastAPI) -> None:
    """``__Host-`` requires ``Path=/``: browsers would drop such a cookie in production only,
    so the mistake is caught in development already."""

    class Request:
        def __init__(self) -> None:
            self.app = app

    response = StarletteResponse()
    with pytest.raises(ValueError, match="cannot be set on path"):
        set_cookie(response, Request(), SESSION_COOKIE, "x", path="/manage")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="cannot be set on path"):
        set_cookie(response, Request(), VOTER_COOKIE, "x")  # type: ignore[arg-type]
