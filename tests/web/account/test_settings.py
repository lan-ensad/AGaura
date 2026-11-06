"""Account settings: name, password, email change, log out everywhere, deletion, account log
(FR-03, FR-05, FR-06, FR-84)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agaura.models import Assembly, OutboxEmail, User, UserSession
from agaura.security import verify_password
from agaura.web.deps import SESSION_COOKIE
from tests.conftest import World
from tests.web.account.conftest import actions, audit_text, emails_to, last_link
from tests.web.conftest import PASSWORD, log_in, make_account, post

NEW_PASSWORD = "a brand new passphrase"


def count(world: World, model: type) -> int:
    with world.database.read() as db:
        return db.scalar(select(func.count()).select_from(model)) or 0


def reload(world: World, user: User) -> User | None:
    with world.database.read() as db:
        return db.get(User, user.id)


def second_device(client: TestClient, email: str = "owner@example.org") -> TestClient:
    other = TestClient(client.app, follow_redirects=False)
    log_in(other, email)
    return other


def test_account_page_requires_login(client: TestClient) -> None:
    response = client.get("/account")
    assert response.status_code == 303 and response.headers["location"] == "/login?next=/account"


def test_account_page(admin: TestClient) -> None:
    page = admin.get("/account")
    assert page.status_code == 200
    assert "Votre compte" in page.text and "owner@example.org" in page.text
    assert "Confirmée" in page.text and "Supprimer le compte" in page.text


class TestName:
    def test_change_name(self, admin: TestClient, world: World, owner: User) -> None:
        response = post(admin, "/account/name", {"display_name": "  Camille   Martin "})
        assert response.status_code == 303
        assert "Votre nom a été enregistré." in admin.get("/account").text
        user = reload(world, owner)
        assert user is not None and user.display_name == "Camille Martin"
        post(admin, "/account/name", {"display_name": ""})
        user = reload(world, owner)
        assert user is not None and user.display_name is None
        assert actions(world, owner.id).count("account.display_name_changed") == 2

    def test_too_long(self, admin: TestClient) -> None:
        response = post(admin, "/account/name", {"display_name": "x" * 101})
        assert response.status_code == 400 and "trop long" in response.text


class TestPassword:
    def test_change_ends_the_other_sessions(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        other = second_device(admin)
        response = post(
            admin,
            "/account/password",
            {
                "current_password": PASSWORD,
                "new_password": NEW_PASSWORD,
                "new_password_confirm": NEW_PASSWORD,
            },
        )
        assert response.status_code == 303
        assert "Vos autres appareils ont été déconnectés." in admin.get("/account").text
        assert other.get("/account").status_code == 303
        assert count(world, UserSession) == 1
        user = reload(world, owner)
        assert user is not None and verify_password(user.password_hash, NEW_PASSWORD)
        assert "account.password_changed" in actions(world, owner.id)
        assert NEW_PASSWORD not in audit_text(world) and PASSWORD not in audit_text(world)

    def test_change_renews_the_login_cookie(self, admin: TestClient, world: World) -> None:
        """A copy of the cookie taken before the change (stolen, left on a shared computer)
        stops working; this browser stays logged in with a new one."""
        stolen = TestClient(admin.app, follow_redirects=False)
        stolen.cookies.set(SESSION_COOKIE, admin.cookies[SESSION_COOKIE])
        assert stolen.get("/account").status_code == 200
        before = admin.cookies[SESSION_COOKIE]
        data = {
            "current_password": PASSWORD,
            "new_password": NEW_PASSWORD,
            "new_password_confirm": NEW_PASSWORD,
        }
        assert post(admin, "/account/password", data).status_code == 303
        assert admin.cookies[SESSION_COOKIE] != before
        assert admin.get("/account").status_code == 200
        assert stolen.get("/account").status_code == 303
        assert count(world, UserSession) == 1

    def test_wrong_current_password(self, admin: TestClient, world: World, owner: User) -> None:
        data = {
            "current_password": "not my password",
            "new_password": NEW_PASSWORD,
            "new_password_confirm": NEW_PASSWORD,
        }
        response = post(admin, "/account/password", data)
        assert response.status_code == 400 and "Mot de passe incorrect." in response.text
        user = reload(world, owner)
        assert user is not None and verify_password(user.password_hash, PASSWORD)

    def test_new_password_rules(self, admin: TestClient) -> None:
        short = {
            "current_password": PASSWORD,
            "new_password": "short",
            "new_password_confirm": "short",
        }
        assert "trop court" in post(admin, "/account/password", short).text
        mismatch = {
            "current_password": PASSWORD,
            "new_password": NEW_PASSWORD,
            "new_password_confirm": NEW_PASSWORD + "!",
        }
        assert "ne correspondent pas" in post(admin, "/account/password", mismatch).text

    def test_wrong_passwords_are_rate_limited(self, admin: TestClient) -> None:
        data = {
            "current_password": "not my password",
            "new_password": NEW_PASSWORD,
            "new_password_confirm": NEW_PASSWORD,
        }
        for _ in range(10):
            assert post(admin, "/account/password", data).status_code == 400
        blocked = post(admin, "/account/password", {**data, "current_password": PASSWORD})
        assert blocked.status_code == 429
        deletion = post(admin, "/account/delete", {"delete_password": PASSWORD})
        assert deletion.status_code == 429


class TestEmailChange:
    def change(self, client: TestClient, email: str, password: str = PASSWORD):  # type: ignore[no-untyped-def]
        return post(client, "/account/email", {"new_email": email, "email_password": password})

    def test_confirmed_by_the_new_address(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        response = self.change(admin, "New.Address@example.org")
        assert response.status_code == 303
        page = admin.get("/account").text
        assert "Un lien de confirmation a été envoyé à la nouvelle adresse." in page
        assert "En attente de confirmation depuis new.address@example.org" in page
        user = reload(world, owner)
        assert user is not None and user.email == "owner@example.org"  # not yet

        (email,) = emails_to(world, "new.address@example.org")
        assert email.subject == "Confirmez votre nouvelle adresse e-mail"
        link = last_link(world, "new.address@example.org")
        assert link.startswith("/account/email/")
        confirmed = admin.get(link)
        assert confirmed.status_code == 303 and confirmed.headers["location"] == "/account"
        assert "Votre adresse e-mail a été modifiée." in admin.get("/account").text
        user = reload(world, owner)
        assert user is not None and user.email == "new.address@example.org"

        requested, changed = emails_to(world, "owner@example.org")
        assert requested.subject == "Demande de changement de votre adresse e-mail"
        assert changed.subject == "Votre adresse e-mail a été modifiée"
        assert "new.address@example.org" in changed.body_text
        assert "account.email_change_requested" in actions(world, owner.id)
        assert "account.email_changed" in actions(world, owner.id)
        assert admin.get(link).status_code == 400  # single use
        assert post(admin, "/logout").status_code == 303
        log_in(admin, "new.address@example.org")

    def test_current_address_is_told_at_once(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        self.change(admin, "new@example.org")
        (notice,) = emails_to(world, "owner@example.org")
        assert notice.subject == "Demande de changement de votre adresse e-mail"
        assert "la nouvelle adresse serait new@example.org" in notice.body_text
        assert "«\u00a0Se déconnecter partout\u00a0»" in notice.body_text
        assert "/account/email/" not in notice.body_text  # the link goes to the new address only

    @pytest.mark.parametrize("action", ["password", "logout-everywhere"])
    def test_cancelled_by_a_password_change_or_logging_out_everywhere(
        self, admin: TestClient, world: World, owner: User, action: str
    ) -> None:
        self.change(admin, "intruder@example.org")
        link = last_link(world, "intruder@example.org")
        if action == "password":
            data = {
                "current_password": PASSWORD,
                "new_password": NEW_PASSWORD,
                "new_password_confirm": NEW_PASSWORD,
            }
            assert post(admin, "/account/password", data).status_code == 303
            assert "En attente de confirmation" not in admin.get("/account").text
        else:
            assert post(admin, "/account/logout-everywhere").status_code == 303
        intruder = TestClient(admin.app, follow_redirects=False)
        response = intruder.get(link)
        assert response.status_code == 400 and "déjà été utilisé" in response.text
        user = reload(world, owner)
        assert user is not None and user.email == "owner@example.org"
        assert "account.email_changed" not in actions(world, owner.id)

    @pytest.mark.parametrize("address", ["a@x.org,b@y.org", "x\n@y.org", "<a@x.org>"])
    def test_unsafe_address(self, admin: TestClient, world: World, address: str) -> None:
        response = self.change(admin, address)
        assert response.status_code == 400
        assert "Cette adresse e-mail est invalide." in response.text
        with world.database.read() as db:
            assert db.scalar(select(func.count()).select_from(OutboxEmail)) == 0

    def test_link_works_from_another_browser(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        self.change(admin, "new@example.org")
        browser = TestClient(admin.app, follow_redirects=False)
        response = browser.get(last_link(world, "new@example.org"))
        assert response.headers["location"] == "/login?next=/account"

    def test_wrong_password(self, admin: TestClient, world: World) -> None:
        response = self.change(admin, "new@example.org", "not my password")
        assert response.status_code == 400 and "Mot de passe incorrect." in response.text
        assert 'value="new@example.org"' in response.text
        assert emails_to(world, "new@example.org") == []

    def test_taken_address_gets_the_same_answer(self, admin: TestClient, world: World) -> None:
        """The form never tells whether an address has an account: the owner of the taken
        address is told by email instead, and gets no link."""
        make_account(world, "taken@example.org")
        for address in ("free@example.org", "Taken@example.org"):
            response = self.change(admin, address)
            assert (response.status_code, response.headers["location"]) == (303, "/account")
            page = admin.get("/account").text
            assert "Un lien de confirmation a été envoyé à la nouvelle adresse." in page
            assert f"En attente de confirmation depuis {address.lower()}" in page
        (told,) = emails_to(world, "taken@example.org")
        assert told.subject == "Votre adresse e-mail a été indiquée pour un autre compte AGaura"
        assert "/account/email/" not in told.body_text
        notices = emails_to(world, "owner@example.org")
        assert [notice.subject for notice in notices] == [
            "Demande de changement de votre adresse e-mail"
        ] * 2

    def test_same_or_invalid_address(self, admin: TestClient, world: World) -> None:
        same = self.change(admin, "OWNER@example.org")
        assert same.status_code == 400 and "est déjà votre adresse e-mail." in same.text
        invalid = self.change(admin, "nope")
        assert invalid.status_code == 400

    def test_address_taken_meanwhile(self, admin: TestClient, world: World, owner: User) -> None:
        self.change(admin, "new@example.org")
        make_account(world, "new@example.org")
        response = admin.get(last_link(world, "new@example.org"))
        assert response.status_code == 400
        user = reload(world, owner)
        assert user is not None and user.email == "owner@example.org"

    def test_requests_are_rate_limited(self, admin: TestClient) -> None:
        for number in range(5):
            assert self.change(admin, f"new{number}@example.org").status_code == 303
        assert self.change(admin, "new9@example.org").status_code == 429

    def test_every_request_counts(self, admin: TestClient, world: World) -> None:
        """Refused requests count too, so the form cannot test addresses without limit."""
        make_account(world, "taken@example.org")
        assert self.change(admin, "taken@example.org", "not my password").status_code == 400
        assert self.change(admin, "nope").status_code == 400
        assert self.change(admin, "OWNER@example.org").status_code == 400
        for number in range(2):
            assert self.change(admin, f"new{number}@example.org").status_code == 303
        assert self.change(admin, "taken@example.org").status_code == 429
        assert emails_to(world, "taken@example.org") == []

    def test_invalid_link(self, client: TestClient) -> None:
        response = client.get("/account/email/unknown")
        assert response.status_code == 400 and 'href="/account"' in response.text


def test_log_out_everywhere(admin: TestClient, world: World, owner: User) -> None:
    other = second_device(admin)
    response = post(admin, "/account/logout-everywhere")
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert "déconnecté·e de tous vos appareils" in admin.get("/login").text
    assert admin.get("/account").status_code == 303
    assert other.get("/account").status_code == 303
    assert count(world, UserSession) == 0
    assert "account.logged_out_everywhere" in actions(world, owner.id)


class TestDeletion:
    def test_requires_the_password(self, admin: TestClient, world: World) -> None:
        response = post(admin, "/account/delete", {"delete_password": "not my password"})
        assert response.status_code == 400 and "Mot de passe incorrect." in response.text
        assert count(world, User) == 1

    def test_refused_while_a_session_is_in_session(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        world.assembly(owner, in_session=True)
        response = post(admin, "/account/delete", {"delete_password": PASSWORD})
        assert response.status_code == 400 and "Une de vos sessions est en cours." in response.text
        assert count(world, User) == 1

    def test_deletes_everything(self, admin: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=True)
        world.poll(owner, public_id)
        world.voters(owner, public_id, 2)
        with world.database.write() as db:
            assembly = db.scalar(select(Assembly))
            assert assembly is not None
            from agaura.services import polls

            for poll in assembly.polls:
                polls.close_poll(db, owner, poll.id)
        with world.database.write() as db:
            from agaura.services import assemblies

            assemblies.close_assembly(db, owner, public_id)
        world.assembly(owner, in_session=False)
        post(admin, "/account/email", {"new_email": "new@example.org", "email_password": PASSWORD})
        other = make_account(world, "other@example.org")
        world.assembly(other, in_session=False)

        response = post(admin, "/account/delete", {"delete_password": PASSWORD})
        assert response.status_code == 303 and response.headers["location"] == "/"
        assert "Votre compte et toutes ses sessions ont été supprimés." in admin.get("/").text
        assert admin.get("/account").status_code == 303

        assert reload(world, owner) is None
        assert count(world, Assembly) == 1  # the other account's session stays
        assert count(world, UserSession) == 0
        with world.database.read() as db:
            addresses = set(db.scalars(select(OutboxEmail.to_address)))
        assert addresses.isdisjoint({"owner@example.org", "new@example.org"})
        assert actions(world, owner.id)[-1] == "account.deleted"


def test_account_log_shows_events_without_secrets(
    client: TestClient, world: World, owner: User
) -> None:
    post(client, "/login", {"email": owner.email, "password": "not my password"})
    log_in(client)
    post(client, "/account/name", {"display_name": "Camille"})
    post(client, "/account/email", {"new_email": "new@example.org", "email_password": PASSWORD})
    page = client.get("/account").text
    log = page[page.index("Journal du compte") :]
    for label in [
        "Échec de connexion",
        "mot de passe erroné",
        "Connexion",
        "Nom modifié",
        "Changement d&#39;adresse e-mail demandé",
    ]:
        assert label in log, label
    assert log.index("Changement") < log.index("Nom modifié") < log.index("Échec")
    link_token = last_link(world, "new@example.org").split("/")[-1]
    assert PASSWORD not in page and link_token not in page
    assert PASSWORD not in audit_text(world) and link_token not in audit_text(world)


def test_operator_actions_appear_in_the_account_log(
    admin: TestClient, world: World, owner: User
) -> None:
    from agaura.services import accounts

    with world.database.write() as db:
        accounts.set_quotas(db, owner.email, {"open_assemblies": 3})
    page = admin.get("/account").text
    assert "Limites du compte modifiées" in page and "par l&#39;exploitant" in page
