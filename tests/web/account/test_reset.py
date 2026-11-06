"""Password reset by email (FR-04)."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agaura.models import EmailToken, User, UserSession
from agaura.security import verify_password
from tests.conftest import World
from tests.web.account.conftest import actions, audit_text, emails_to, last_link
from tests.web.conftest import PASSWORD, log_in, post

NEW_PASSWORD = "a brand new passphrase"


def request_reset(client: TestClient, email: str) -> str:
    response = post(client, "/reset", {"email": email})
    assert response.status_code == 200
    return response.text


def login_sessions(world: World) -> int:
    with world.database.read() as db:
        return db.scalar(select(func.count()).select_from(UserSession)) or 0


def test_form(client: TestClient) -> None:
    page = client.get("/reset")
    assert page.status_code == 200 and "Mot de passe oublié" in page.text


def test_same_answer_whether_or_not_the_account_exists(
    client: TestClient, world: World, owner: User
) -> None:
    known = request_reset(client, "Owner@Example.org")
    unknown = request_reset(client, "nobody@example.org")
    assert known == unknown
    assert "Consultez votre messagerie" in known
    assert len(emails_to(world, "owner@example.org")) == 1
    assert emails_to(world, "nobody@example.org") == []
    assert actions(world, owner.id) == ["account.password_reset_requested"]


def test_reset_sets_the_password_and_ends_every_session(
    client: TestClient, world: World, owner: User
) -> None:
    log_in(client)
    other_device = TestClient(client.app, follow_redirects=False)
    log_in(other_device)
    assert login_sessions(world) == 2

    request_reset(client, owner.email)
    (email,) = emails_to(world, owner.email)
    assert email.subject == "Réinitialisez votre mot de passe" and "dans l'heure" in email.body_text
    link = last_link(world, owner.email)

    form = client.get(link)
    assert form.status_code == 200 and "Nouveau mot de passe" in form.text
    assert form.headers["referrer-policy"] == "no-referrer"
    response = post(client, link, {"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD})
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert "Connectez-vous avec le nouveau" in client.get("/login").text

    assert login_sessions(world) == 0
    assert other_device.get("/account").status_code == 303  # logged out
    with world.database.read() as db:
        user = db.get(User, owner.id)
        assert user is not None and verify_password(user.password_hash, NEW_PASSWORD)
    assert post(client, "/login", {"email": owner.email, "password": PASSWORD}).status_code == 400
    assert (
        post(client, "/login", {"email": owner.email, "password": NEW_PASSWORD}).status_code == 303
    )
    assert "account.password_reset" in actions(world, owner.id)
    assert NEW_PASSWORD not in audit_text(world) and link.split("/")[-1] not in audit_text(world)

    used = client.get(link)
    assert used.status_code == 400 and "déjà été utilisé" in used.text
    again = post(
        client,
        link,
        {"password": "yet another passphrase", "password_confirm": "yet another passphrase"},
    )
    assert again.status_code == 400


def test_invalid_new_password_keeps_the_link(client: TestClient, world: World, owner: User) -> None:
    request_reset(client, owner.email)
    link = last_link(world, owner.email)
    short = post(client, link, {"password": "short", "password_confirm": "short"})
    assert short.status_code == 400 and "trop court" in short.text
    mismatch = post(client, link, {"password": NEW_PASSWORD, "password_confirm": "other one!!"})
    assert mismatch.status_code == 400 and "ne correspondent pas" in mismatch.text
    response = post(client, link, {"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD})
    assert response.status_code == 303


def test_link_expires_after_one_hour(client: TestClient, world: World, owner: User) -> None:
    request_reset(client, owner.email)
    link = last_link(world, owner.email)
    with world.database.write() as db:
        token = db.scalar(select(EmailToken))
        assert token is not None
        token.expires_at -= timedelta(hours=1)
    page = client.get(link)
    assert page.status_code == 400 and "Ce lien a expiré." in page.text
    assert 'href="/reset"' in page.text
    response = post(client, link, {"password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD})
    assert response.status_code == 400 and "Ce lien a expiré." in response.text


def test_a_new_request_voids_the_previous_link(
    client: TestClient, world: World, owner: User
) -> None:
    request_reset(client, owner.email)
    first = last_link(world, owner.email)
    request_reset(client, owner.email)
    assert client.get(first).status_code == 400
    assert client.get(last_link(world, owner.email)).status_code == 200


def test_unknown_link(client: TestClient) -> None:
    assert client.get("/reset/unknown-token").status_code == 400


def test_invalid_email(client: TestClient) -> None:
    response = post(client, "/reset", {"email": "not an email"})
    assert response.status_code == 400 and "invalide" in response.text


def test_rate_limit_per_email_is_silent(client: TestClient, world: World, owner: User) -> None:
    answers = {request_reset(client, owner.email) for _ in range(5)}
    assert len(answers) == 1
    assert len(emails_to(world, owner.email)) == 3


def test_requests_from_elsewhere_do_not_use_up_the_owner_s(
    client: TestClient, world: World, owner: User
) -> None:
    """Someone asking for resets of the owner's address from another network cannot stop the
    owner's own requests from being sent."""
    attacker = TestClient(client.app, follow_redirects=False, client=("198.51.100.7", 50000))
    for _ in range(5):
        request_reset(attacker, owner.email)
    assert len(emails_to(world, owner.email)) == 3
    request_reset(client, owner.email)
    assert len(emails_to(world, owner.email)) == 4


def test_a_mailbox_receives_at_most_ten_resets_an_hour(
    client: TestClient, world: World, owner: User
) -> None:
    answers = set()
    for number in range(6):
        other = TestClient(client.app, follow_redirects=False, client=(f"198.51.100.{number}", 1))
        answers.update(request_reset(other, owner.email) for _ in range(3))
    assert len(answers) == 1
    sent = emails_to(world, owner.email)
    assert len(sent) == 10
    # Capped, the latest link still works: it lasts as long as the window.
    assert client.get(last_link(world, owner.email)).status_code == 200


def test_rate_limit_per_ip(client: TestClient) -> None:
    for number in range(30):
        request_reset(client, f"person{number}@example.org")
    blocked = post(client, "/reset", {"email": "one-more@example.org"})
    assert blocked.status_code == 429 and "Trop de tentatives" in blocked.text


def test_disabled_account_gets_no_link(client: TestClient, world: World, owner: User) -> None:
    with world.database.write() as db:
        user = db.get(User, owner.id)
        assert user is not None
        user.disabled_at = user.created_at
    request_reset(client, owner.email)
    assert emails_to(world, owner.email) == []
