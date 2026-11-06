"""Signup with the anti-bot proof of work (FR-01) and email verification (FR-02), in the open
signup mode (``AGAURA_SIGNUP=open``); the closed mode is in ``test_signup_closed.py``."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agaura.config import Settings, SignupMode
from agaura.models import EmailToken, User
from agaura.security import Keyring, hash_token
from agaura.web import pow as proof_of_work
from tests.conftest import World
from tests.web.account.conftest import actions, altcha, emails_to, last_link, sign_up
from tests.web.conftest import PASSWORD, make_account, post


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"signup": SignupMode.OPEN})


def user_by_email(world: World, email: str) -> User | None:
    with world.database.read() as db:
        return db.scalar(select(User).where(User.email == email))


class TestSignup:
    def test_page_loads_the_widget(self, client: TestClient) -> None:
        page = client.get("/signup")
        assert page.status_code == 200
        assert "Créer un compte" in page.text
        assert 'data-challenge-url="/altcha/challenge"' in page.text
        assert "/static/js/pow.js" in page.text and 'name="altcha"' in page.text
        assert client.get("/static/js/pow.js").status_code == 200
        assert client.get("/static/css/account.css").status_code == 200

    def test_challenge_follows_the_altcha_format(self, client: TestClient) -> None:
        response = client.get("/altcha/challenge")
        assert response.headers["cache-control"] == "no-store"
        challenge = response.json()
        assert set(challenge) == {"algorithm", "challenge", "maxnumber", "salt", "signature"}
        assert challenge["algorithm"] == "SHA-256"
        assert challenge["maxnumber"] == proof_of_work.MAX_NUMBER
        assert "?expires=" in challenge["salt"]

    def test_signup_logs_in_and_sends_a_verification_link(
        self, client: TestClient, world: World
    ) -> None:
        response = sign_up(client, " New@Example.org ", display_name=" Camille ")
        assert response.status_code == 303 and response.headers["location"] == "/dashboard"
        user = user_by_email(world, "new@example.org")
        assert user is not None and user.display_name == "Camille"
        assert user.email_verified_at is None
        assert actions(world, user.id) == ["account.created"]

        account = client.get("/account")  # logged in
        assert account.status_code == 200
        assert "Votre compte est créé." in account.text
        assert "pas encore confirmée" in account.text

        (email,) = emails_to(world, "new@example.org")
        assert email.subject == "Confirmez votre adresse e-mail"
        assert "Bonjour Camille," in email.body_text and "24 heures" in email.body_text
        with world.database.read() as db:
            token = db.scalar(select(EmailToken))
        link_token = last_link(world, "new@example.org").removeprefix("/verify/")
        assert token is not None and token.token_hash == hash_token(link_token)
        assert token.token_hash not in email.body_text

    def test_logged_in_visitor_goes_to_the_dashboard(self, admin: TestClient) -> None:
        assert admin.get("/signup").headers["location"] == "/dashboard"

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"password": "short", "password_confirm": "short"}, "trop court"),
            ({"password_confirm": "something else entirely"}, "ne correspondent pas"),
            ({"email": "not-an-email"}, "adresse e-mail est invalide"),
            ({"email": "new@example.org,b@y.org"}, "adresse e-mail est invalide"),
            ({"email": "new\n@example.org"}, "adresse e-mail est invalide"),
            ({"display_name": "x" * 101}, "trop long"),
        ],
    )
    def test_invalid_forms_are_refused(
        self, client: TestClient, world: World, fields: dict[str, str], message: str
    ) -> None:
        response = sign_up(client, **fields)
        assert response.status_code == 400 and message in response.text
        assert user_by_email(world, "new@example.org") is None

    def test_taken_address_gets_a_neutral_message(self, client: TestClient, world: World) -> None:
        make_account(world, "taken@example.org")
        response = sign_up(client, "Taken@example.org")
        assert response.status_code == 400
        assert "ne peut pas être utilisée" in response.text
        assert "existe déjà" not in response.text
        assert emails_to(world, "taken@example.org") == []

    def test_refused_without_proof_of_work(self, client: TestClient, world: World) -> None:
        for payload in ["", "not base64!", "e30="]:
            response = sign_up(client, altcha=payload)
            assert response.status_code == 400 and "anti-robot" in response.text
        assert user_by_email(world, "new@example.org") is None

    def test_refused_with_a_wrong_number(self, client: TestClient) -> None:
        challenge = client.get("/altcha/challenge").json()
        number = proof_of_work.solve(challenge)
        assert number is not None
        wrong = proof_of_work.encode_solution(challenge, (number + 1) % 2000)
        assert sign_up(client, altcha=wrong).status_code == 400

    def test_refused_with_a_forged_challenge(self, client: TestClient) -> None:
        forged = proof_of_work.create_challenge(Keyring("another-secret-key-0123456789abcdef"))
        number = proof_of_work.solve(forged)
        assert number is not None
        response = sign_up(client, altcha=proof_of_work.encode_solution(forged, number))
        assert response.status_code == 400

    def test_refused_with_an_expired_challenge(self, client: TestClient) -> None:
        keyring = client.app.state.keyring  # type: ignore[attr-defined]
        old = proof_of_work.create_challenge(
            keyring, now=time.time() - proof_of_work.CHALLENGE_LIFETIME - 1
        )
        number = proof_of_work.solve(old)
        assert number is not None
        response = sign_up(client, altcha=proof_of_work.encode_solution(old, number))
        assert response.status_code == 400 and "anti-robot" in response.text

    def test_a_solution_works_once(self, client: TestClient, world: World) -> None:
        solution = altcha(client)
        assert sign_up(client, "first@example.org", altcha=solution).status_code == 303
        post(client, "/logout")
        reused = sign_up(client, "second@example.org", altcha=solution)
        assert reused.status_code == 400 and "anti-robot" in reused.text
        assert user_by_email(world, "second@example.org") is None

    def test_signups_are_rate_limited_per_ip(self, client: TestClient) -> None:
        for number in range(10):
            response = sign_up(client, f"user{number}@example.org")
            assert response.status_code == 303
            post(client, "/logout")
        blocked = sign_up(client, "one-more@example.org")
        assert blocked.status_code == 429 and "Trop de tentatives" in blocked.text


class TestVerification:
    def test_link_confirms_the_address_once(self, client: TestClient, world: World) -> None:
        sign_up(client)
        link = last_link(world, "new@example.org")
        response = client.get(link)
        assert response.status_code == 303 and response.headers["location"] == "/dashboard"
        user = user_by_email(world, "new@example.org")
        assert user is not None and user.email_verified_at is not None
        assert "account.email_verified" in actions(world, user.id)
        account = client.get("/account").text
        assert "Votre adresse e-mail est confirmée." in account
        assert "pas encore confirmée" not in account

        again = client.get(link)
        assert again.status_code == 400
        assert "déjà été utilisé" in again.text
        assert again.headers["referrer-policy"] == "no-referrer"

    def test_link_works_without_being_logged_in(self, client: TestClient, world: World) -> None:
        sign_up(client)
        post(client, "/logout")
        response = client.get(last_link(world, "new@example.org"))
        assert response.headers["location"] == "/login?next=/dashboard"
        assert "Votre adresse e-mail est confirmée." in client.get("/login").text

    def test_expired_link(self, client: TestClient, world: World) -> None:
        sign_up(client)
        expire_tokens(world)
        response = client.get(last_link(world, "new@example.org"))
        assert response.status_code == 400 and "Ce lien a expiré." in response.text
        user = user_by_email(world, "new@example.org")
        assert user is not None and user.email_verified_at is None

    def test_unknown_link(self, client: TestClient) -> None:
        response = client.get("/verify/" + "x" * 43)
        assert response.status_code == 400 and "Lien non valable" in response.text
        assert client.get("/verify/" + "x" * 300).status_code in {400, 404}

    def test_resend_replaces_the_link(self, client: TestClient, world: World) -> None:
        sign_up(client)
        first = last_link(world, "new@example.org")
        response = post(client, "/account/verify/resend")
        assert response.status_code == 303 and response.headers["location"] == "/account"
        assert "Un nouveau lien de confirmation" in client.get("/account").text
        second = last_link(world, "new@example.org")
        assert second != first and len(emails_to(world, "new@example.org")) == 2
        assert client.get(first).status_code == 400  # only the latest link works
        assert client.get(second).status_code == 303

    def test_resend_is_rate_limited(self, client: TestClient, world: World) -> None:
        sign_up(client)
        for _ in range(3):
            assert post(client, "/account/verify/resend").status_code == 303
        blocked = post(client, "/account/verify/resend")
        assert blocked.status_code == 429 and "Trop de tentatives" in blocked.text
        assert len(emails_to(world, "new@example.org")) == 4

    def test_resend_when_already_confirmed(self, admin: TestClient, world: World) -> None:
        response = post(admin, "/account/verify/resend")
        assert response.status_code == 303
        assert emails_to(world, "owner@example.org") == []

    def test_resend_requires_login(self, client: TestClient) -> None:
        response = post(client, "/account/verify/resend")
        assert response.status_code == 303 and response.headers["location"].startswith("/login")


def expire_tokens(world: World) -> None:
    """Move every email link two days into the past."""
    with world.database.write() as db:
        for token in db.scalars(select(EmailToken)):
            token.expires_at -= timedelta(days=2)


def test_password_is_not_stored_in_clear(client: TestClient, world: World) -> None:
    sign_up(client)
    user = user_by_email(world, "new@example.org")
    assert user is not None and PASSWORD not in user.password_hash
