"""The accounts page: list, invitations, new links, deletion (super-admin)."""

from __future__ import annotations

import html
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select

from agaura.models import AuditEntry, EmailToken, OutboxEmail, User
from agaura.services import accounts
from agaura.web import superadmin
from tests.conftest import World
from tests.web.conftest import PASSWORD, make_account, post

INVITE_LINK = re.compile(r"http://testserver(/invite/[A-Za-z0-9_-]+)")


def emails_to(world: World, address: str) -> list[OutboxEmail]:
    with world.database.read() as db:
        return list(
            db.scalars(
                select(OutboxEmail)
                .where(OutboxEmail.to_address == address)
                .order_by(OutboxEmail.id)
            )
        )


def invitation_link(world: World, address: str) -> str:
    sent = emails_to(world, address)
    assert sent, f"no email to {address}"
    match = INVITE_LINK.search(sent[-1].body_text)
    assert match, sent[-1].body_text
    return match.group(1)


def page(root: TestClient) -> str:
    """The accounts page as text, apostrophes of flashed messages unescaped."""
    return html.unescape(root.get("/superadmin").text)


def account(world: World, email: str) -> User | None:
    with world.database.read() as db:
        return db.scalar(select(User).where(User.email == email))


def account_id(world: World, email: str) -> int:
    found = account(world, email)
    assert found is not None
    return found.id


def invite(root: TestClient, email: str = "new@example.org", name: str = "") -> None:
    response = post(root, "/superadmin/invite", {"email": email, "display_name": name})
    assert response.status_code == 303 and response.headers["location"] == "/superadmin"


class TestList:
    def test_states_and_no_session_data(
        self, root: TestClient, world: World, superadmin: User
    ) -> None:
        holder = make_account(world, "holder@example.org")
        public_id = world.assembly(holder, title="Titre très confidentiel")
        disabled = make_account(world, "off@example.org")
        with world.database.write() as db:
            accounts.set_disabled(db, disabled.email, True)
        invite(root, "pending@example.org", "Camille Martin")
        response = root.get("/superadmin")
        assert response.status_code == 200
        text = html.unescape(response.text)
        for expected in [
            "holder@example.org",
            "off@example.org",
            "pending@example.org",
            "Camille Martin",
            "Invitation en attente",
            "Désactivé",
            "Actif",
            "Super-admin",
            "(ce compte)",
            "Le lien d'invitation est encore valable.",
            "aucune connexion",
            "/static/css/superadmin.css",
        ]:
            assert expected in text, expected
        assert "Titre très confidentiel" not in text and public_id not in text
        assert "/manage/" not in text and 'href="/dashboard"' not in text
        # The super-admin's own row has no action; the others can be deleted.
        assert f"/superadmin/accounts/{superadmin.id}/" not in text
        assert f"/superadmin/accounts/{holder.id}/delete" in text
        assert f"/superadmin/accounts/{holder.id}/resend" not in text  # already active

    def test_expired_invitation_is_shown(self, root: TestClient, world: World) -> None:
        invite(root)
        with world.database.write() as db:
            for token in db.scalars(select(EmailToken)):
                token.used_at = token.expires_at  # same effect for the list: no valid link
        assert "Le lien d'invitation a expiré." in page(root)


class TestInvite:
    def test_creates_the_account_and_sends_the_link(
        self, root: TestClient, world: World, superadmin: User
    ) -> None:
        invite(root, " New@Example.org ", " Camille ")
        assert "Compte créé. L'invitation est envoyée" in page(root)
        user = account(world, "new@example.org")
        assert user is not None and user.password_hash is None
        assert user.display_name == "Camille" and not user.is_superadmin
        (email,) = emails_to(world, "new@example.org")
        assert email.subject == "Votre compte AGaura"
        assert "Bonjour Camille," in email.body_text and "7 jours" in email.body_text
        link = invitation_link(world, "new@example.org")
        with world.database.read() as db:
            entry = db.scalar(select(AuditEntry).order_by(AuditEntry.id.desc()))
        assert entry is not None and entry.action == "account.invited"
        assert entry.details == {"by": superadmin.id} and link not in str(entry.details)

    @pytest.mark.parametrize(
        ("email", "message"),
        [
            ("not-an-email", "adresse e-mail est invalide"),
            ("a@x.org,b@y.org", "adresse e-mail est invalide"),
            ("x\r\n@y.org", "adresse e-mail est invalide"),
            ("root@example.org", "existe déjà"),
        ],
    )
    def test_refused(self, root: TestClient, world: World, email: str, message: str) -> None:
        response = post(root, "/superadmin/invite", {"email": email, "display_name": "Bob"})
        assert response.status_code == 400 and message in response.text
        assert 'value="Bob"' in response.text  # the form keeps what was typed
        assert emails_to(world, email) == []

    def test_rate_limited(
        self, root: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(superadmin, "INVITATIONS_PER_SUPERADMIN", (2, 3600.0))
        invite(root, "a@example.org")
        invite(root, "b@example.org")
        blocked = post(root, "/superadmin/invite", {"email": "c@example.org"})
        assert blocked.status_code == 429 and "Trop de tentatives" in blocked.text
        user = account_id(world, "a@example.org")
        resend = post(root, f"/superadmin/accounts/{user}/resend")
        assert resend.status_code == 303
        assert "Trop de tentatives" in page(root)


class TestResend:
    def test_new_link_replaces_the_old_one(self, root: TestClient, world: World) -> None:
        invite(root)
        first = invitation_link(world, "new@example.org")
        user = account_id(world, "new@example.org")
        response = post(root, f"/superadmin/accounts/{user}/resend")
        assert response.status_code == 303 and response.headers["location"] == "/superadmin"
        assert "Une nouvelle invitation est envoyée." in page(root)
        second = invitation_link(world, "new@example.org")
        assert second != first
        assert root.get(first).status_code == 400
        assert root.get(second).status_code == 200

    def test_refused_for_an_active_account(self, root: TestClient, world: World) -> None:
        holder = make_account(world, "holder@example.org")
        post(root, f"/superadmin/accounts/{holder.id}/resend")
        assert "Ce compte n'attend pas d'invitation" in page(root)
        assert emails_to(world, "holder@example.org") == []

    def test_unknown_account(self, root: TestClient) -> None:
        assert post(root, "/superadmin/accounts/999/resend").status_code == 404


def delete(root: TestClient, account_id: int, email: str, password: str = PASSWORD) -> Response:
    return post(
        root,
        f"/superadmin/accounts/{account_id}/delete",
        {"confirm_email": email, "password": password},
    )


class TestDelete:
    def test_deletes_the_account_and_its_sessions(self, root: TestClient, world: World) -> None:
        holder = make_account(world, "holder@example.org")
        world.assembly(holder, in_session=False)
        text = page(root)
        assert f'id="password-{holder.id}" name="password" type="password"' in text
        wrong = delete(root, holder.id, "other@example.org")
        assert wrong.headers["location"] == "/superadmin"
        assert "ne correspond pas au compte" in page(root)
        assert account(world, "holder@example.org") is not None
        response = delete(root, holder.id, "Holder@example.org ")
        assert response.status_code == 303
        assert "Compte supprimé, avec toutes ses sessions." in page(root)
        assert account(world, "holder@example.org") is None

    def test_needs_the_superadmin_password(
        self, root: TestClient, world: World, app: FastAPI, superadmin: User
    ) -> None:
        holder = make_account(world, "holder@example.org")
        for typed in ["", "wrong password"]:
            response = delete(root, holder.id, holder.email, typed)
            assert response.headers["location"] == "/superadmin"
            assert "Mot de passe incorrect." in page(root)
        assert account(world, "holder@example.org") is not None
        # Wrong passwords count against the super-admin account, as in its settings; a right
        # one is not counted.
        limiter = app.state.limiter
        key = f"account-password:{superadmin.id}"
        assert limiter.count(key, 900) == 2
        for _ in range(8):
            delete(root, holder.id, holder.email, "wrong password")
        refused = delete(root, holder.id, holder.email)
        assert refused.status_code == 429 and "Trop de tentatives" in refused.text
        assert account(world, "holder@example.org") is not None
        limiter.reset(key)
        assert delete(root, holder.id, holder.email).status_code == 303
        assert limiter.count(key, 900) == 0
        assert account(world, "holder@example.org") is None

    def test_refused_while_in_session(self, root: TestClient, world: World) -> None:
        holder = make_account(world, "holder@example.org")
        world.assembly(holder, in_session=True, title="Titre très confidentiel")
        delete(root, holder.id, holder.email)
        text = page(root)
        assert "Une session de ce compte est en cours." in text
        assert "Titre très confidentiel" not in text
        assert account(world, "holder@example.org") is not None

    def test_never_a_superadmin(self, root: TestClient, world: World, superadmin: User) -> None:
        delete(root, superadmin.id, superadmin.email)
        assert "Un compte super-admin ne peut pas être supprimé ici" in page(root)
        assert account(world, superadmin.email) is not None

    def test_unknown_account(self, root: TestClient) -> None:
        assert delete(root, 999, "x@y.org").status_code == 404
        # The password comes first: without it, nothing tells whether the account exists.
        assert delete(root, 999, "x@y.org", "wrong").headers["location"] == "/superadmin"
