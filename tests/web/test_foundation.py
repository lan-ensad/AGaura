"""Web foundation: pages, security headers, CSRF, login, errors, translations, real time."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from agaura.i18n import Translator, missing_translations, translator
from agaura.models import User
from agaura.web.realtime import Hub, live_channel, stream
from agaura.web.security import CSRF_COOKIE, RateLimiter
from agaura.web.templating import percent
from tests.web.conftest import PASSWORD, post


def test_every_interface_string_is_translated() -> None:
    missing = missing_translations()
    assert not missing, "run: uv run python scripts/i18n.py check"


def test_translator_plural_forms() -> None:
    french = Translator({}, {"vote": ("vote", "votes")}, "fr")
    assert [french.ngettext("vote", "votes", n) for n in (0, 1, 2)] == ["vote", "vote", "votes"]
    fallback = Translator({}, {}, "fr")
    assert fallback.ngettext("ballot", "ballots", 2) == "ballots"
    assert fallback.gettext("Untranslated") == "Untranslated"
    assert translator().gettext("Log in") == "Se connecter"


def test_home_page_in_french_with_security_headers(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Rejoindre une session" in response.text
    assert '<html lang="fr">' in response.text
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" not in response.headers  # development
    cookie = response.headers["set-cookie"]
    assert (
        cookie.startswith(f"{CSRF_COOKIE}=")
        and "HttpOnly" in cookie
        and "SameSite=lax" in cookie.replace("Lax", "lax")
    )


def test_static_files_and_errors(client: TestClient) -> None:
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/static/missing.css").status_code == 404
    missing = client.get("/manage/nothing/here/at/all")
    assert missing.status_code == 404 and "Page introuvable" in missing.text


def test_join_field_redirects_to_the_session_page(client: TestClient) -> None:
    response = client.get("/join", params={"session": "k7m4-qx"})
    assert response.status_code == 303 and response.headers["location"] == "/K7M4QX"
    invalid = client.get("/join", params={"session": "nope"})
    assert invalid.status_code == 404 and "Identifiant de la session" in invalid.text


class TestCsrf:
    def test_post_without_token_is_refused(self, client: TestClient) -> None:
        client.get("/")
        response = client.post("/login", data={"email": "a@example.org", "password": "x"})
        assert response.status_code == 403
        assert "Accès refusé" in response.text

    def test_post_with_a_wrong_token_is_refused(self, client: TestClient) -> None:
        client.get("/")
        response = client.post("/login", data={"csrf_token": "x" * 43, "email": "a"})
        assert response.status_code == 403

    def test_header_token_is_accepted(self, client: TestClient) -> None:
        client.get("/")
        response = client.post("/logout", headers={"x-csrf-token": client.cookies[CSRF_COOKIE]})
        assert response.status_code == 303


class TestLogin:
    def test_login_logout(self, client: TestClient, owner: User) -> None:
        assert client.get("/login").status_code == 200
        response = post(
            client,
            "/login",
            {"email": " Owner@Example.org ", "password": PASSWORD, "next": "/account"},
        )
        assert response.status_code == 303 and response.headers["location"] == "/account"
        assert client.get("/login").headers["location"] == "/dashboard"  # already logged in
        home = client.get("/")
        assert "Vous êtes connecté·e." in home.text and "Se déconnecter" in home.text
        assert "Vous êtes connecté·e." not in client.get("/").text  # notice shown once
        response = post(client, "/logout")
        assert response.status_code == 303
        assert "Se connecter" in client.get("/").text

    @pytest.mark.parametrize("target", ["https://evil.example", "//evil.example", "/\\evil"])
    def test_no_open_redirect(self, client: TestClient, owner: User, target: str) -> None:
        response = post(
            client, "/login", {"email": owner.email, "password": PASSWORD, "next": target}
        )
        assert response.headers["location"] == "/dashboard"

    def test_wrong_passwords_then_anti_robot_check(self, client: TestClient, owner: User) -> None:
        """Limits in detail: ``tests/web/account/test_login_limits.py``."""
        for number in range(5):
            response = post(client, "/login", {"email": owner.email, "password": "wrong password"})
            assert response.status_code == 400
            assert "Adresse e-mail ou mot de passe incorrect." in response.text
            assert ("data-pow" in response.text) is (number == 4)
        response = post(client, "/login", {"email": owner.email, "password": PASSWORD})
        assert response.status_code == 400
        assert "une vérification anti-robot est nécessaire" in response.text

    def test_protected_page_redirects_to_login(self, client: TestClient) -> None:
        response = client.get("/dashboard")
        assert response.status_code in {303, 404}  # 404 until the dashboard exists


def test_rate_limiter_window() -> None:
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    for _ in range(3):
        limiter.record("k")
    assert limiter.exceeded("k", 3, 10)
    now[0] = 10.5
    assert not limiter.exceeded("k", 1, 10) and limiter.count("k", 10) == 0
    limiter.record("k")
    limiter.reset("k")
    assert limiter.count("k", 10) == 0


def test_percent_filter() -> None:
    assert percent("3/4") == "75,0 %"
    assert percent(None) == "\u2013"
    assert percent("1/0") == "0,0 %"


def test_hub_delivers_across_threads_and_closes() -> None:
    async def scenario() -> list[str]:
        hub = Hub()
        hub.publish("nobody", "ignored")  # before binding: dropped silently
        hub.bind(asyncio.get_running_loop())
        received: list[str] = []

        async def listen() -> None:
            async for message in stream(hub, live_channel("ABC123")):
                received.append(f"{message.event}:{message.data}")

        task = asyncio.create_task(listen())
        await asyncio.sleep(0)
        assert hub.subscribers(live_channel("ABC123")) == 1
        await asyncio.to_thread(hub.publish, live_channel("ABC123"), "update", "polls")
        hub.publish(live_channel("ABC123"), "update", "results")
        hub.publish(live_channel("OTHER1"), "update", "polls")
        await asyncio.sleep(0.05)
        hub.close()
        await asyncio.wait_for(task, 1)
        assert hub.subscribers(live_channel("ABC123")) == 0
        return received

    assert asyncio.run(scenario()) == ["update:polls", "update:results"]


def test_hub_drops_backlog_of_slow_clients() -> None:
    async def scenario() -> int:
        hub = Hub()
        async with hub.subscribe("c") as queue:
            for _ in range(100):
                hub.publish("c", "update")
            return queue.qsize()

    assert 1 <= asyncio.run(scenario()) <= 32


def test_https_only_headers_in_production(settings, database_path) -> None:  # type: ignore[no-untyped-def]
    from agaura.config import Environment
    from agaura.main import create_app

    production = settings.model_copy(
        update={
            "environment": Environment.PRODUCTION,
            "database_url": f"sqlite:///{database_path}",
        }
    )
    with TestClient(create_app(production)) as client:
        headers = client.get("/").headers
    assert headers["strict-transport-security"].startswith("max-age=")
    assert headers["cross-origin-opener-policy"] == "same-origin"
    assert "Secure" in headers["set-cookie"]
