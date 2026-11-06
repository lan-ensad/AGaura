"""Dashboard and session console."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agaura.models import AccessMode, Assembly, AssemblyStatus, Poll, PollStatus, User
from agaura.services import access, polls
from agaura.web.manage.editor import EditorFields, fields_from_kind
from tests.conftest import RESOLUTION, World
from tests.web.conftest import make_account, post


def poll_rows(world: World) -> list[Poll]:
    with world.database.read() as db:
        rows = list(db.scalars(select(Poll).order_by(Poll.id)))
        for poll in rows:
            _ = poll.options, poll.result
        return rows


def step_two(kind: str, **changes: object) -> dict[str, object]:
    """Step 2 of the poll editor for ``kind``, as the browser sends it, saved as a draft."""
    fields = fields_from_kind(kind).model_dump(exclude=set(EditorFields.BUTTONS))
    return {**fields, "kind": kind, "action": "draft", **changes}


def notice(client: TestClient, url: str) -> str:
    return client.get(url).text


class TestDashboard:
    def test_requires_login(self, client: TestClient) -> None:
        response = client.get("/dashboard")
        assert response.status_code == 303
        assert response.headers["location"] == "/login?next=/dashboard"

    def test_create_and_list(self, admin: TestClient, world: World, owner: User) -> None:
        page = admin.get("/dashboard")
        assert page.status_code == 200 and "Aucune session pour l'instant" in page.text
        # Registered members and quorum are set afterwards, in the details of the session.
        assert 'name="registered_count"' not in page.text
        assert 'name="quorum_rule"' not in page.text
        response = post(
            admin,
            "/dashboard",
            {
                "title": "AG 2026",
                "date": "2026-06-20",
                "place": "Salle des fêtes",
                "access_mode": "password",
            },
        )
        assert response.status_code == 303
        public_id = response.headers["location"].rsplit("/", 1)[1]
        console = admin.get(f"/manage/{public_id}")
        assert "Session créée" in console.text and "20 juin 2026" in console.text
        assert 'id="details-registered"' in console.text and 'id="details-quorum"' in console.text
        with world.database.read() as db:
            assembly = db.scalars(select(Assembly).filter_by(public_id=public_id)).one()
            assert (assembly.registered_count, assembly.quorum_rule) == (None, None)
        listing = admin.get("/dashboard").text
        assert "AG 2026" in listing and "En préparation" in listing

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"title": ""}, "Ce champ est obligatoire."),
            ({"title": "AG", "date": "20/06/2026"}, "Saisissez une date valide."),
        ],
    )
    def test_invalid_creation(
        self, admin: TestClient, fields: dict[str, str], message: str
    ) -> None:
        response = post(admin, "/dashboard", fields)
        assert response.status_code == 400 and message in response.text

    def test_unverified_accounts_cannot_create(self, client: TestClient, world: World) -> None:
        make_account(world, "new@example.org", verified=False)
        post(client, "/login", {"email": "new@example.org", "password": "correct horse battery"})
        page = client.get("/dashboard").text
        assert "Confirmez votre adresse e-mail pour créer des sessions" in page
        response = post(client, "/dashboard", {"title": "AG"})
        assert response.status_code == 400
        assert "Confirmez votre adresse e-mail avant de créer une session." in response.text


class TestConsole:
    def test_password_mode_shows_qr_and_password(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        page = admin.get(f"/manage/{public_id.lower()}")
        assert page.status_code == 303 and page.headers["location"] == f"/manage/{public_id}"
        page = admin.get(f"/manage/{public_id}")
        password = world.password(owner, public_id)
        assert re.fullmatch(r"\d{8}", password)
        # Two groups of four, as on the projection screen (D10); no space in between.
        grouped = f'<p class="secret"><span>{password[:4]}</span><span>{password[4:]}</span></p>'
        assert grouped in page.text and "<svg" in page.text
        assert f"http://testserver/{public_id}" in page.text
        assert "Ouvrir la session" in page.text

    def test_session_lifecycle(self, admin: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False)
        assert post(admin, f"/manage/{public_id}/open").status_code == 303
        assert "La session est ouverte" in admin.get(f"/manage/{public_id}").text
        post(admin, f"/manage/{public_id}/close")
        page = admin.get(f"/manage/{public_id}").text
        assert "La session est terminée. Tous les accès" in page
        # Delete needs the confirmation box.
        post(admin, f"/manage/{public_id}/delete")
        assert "Ce champ est obligatoire." in admin.get(f"/manage/{public_id}").text
        response = post(admin, f"/manage/{public_id}/delete", {"confirm": "yes"})
        assert response.headers["location"] == "/dashboard"
        assert admin.get(f"/manage/{public_id}").status_code == 404

    def test_errors_come_back_as_messages(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        public_id = world.assembly(owner)
        world.poll(owner, public_id)
        post(admin, f"/manage/{public_id}/close")  # a poll is still open
        page = admin.get(f"/manage/{public_id}").text
        assert "Clôturez les votes ouverts avant de terminer la session." in page
        post(admin, f"/manage/{public_id}/delete", {"confirm": "yes"})  # in session
        assert (
            "Impossible pendant que la session est en cours."
            in admin.get(f"/manage/{public_id}").text
        )

    def test_numbers_in_other_scripts_are_refused(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        """``"²".isdigit()`` is true but ``int("²")`` fails: a message, never a 500."""
        public_id = world.assembly(owner)
        for raw in ("²", "٣", "1²"):
            response = post(admin, f"/manage/{public_id}/headcount", {"headcount": raw})
            assert response.status_code == 303, raw
            assert "Saisissez un nombre entier." in admin.get(f"/manage/{public_id}").text

    def test_numbers_too_long_are_refused(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        """Past SQLite's integer range (19 digits) or ``int``'s limit (4,300): a message, never
        a 500."""
        public_id = world.assembly(owner)
        for raw in ("9" * 25, "1" * 5000):
            response = post(admin, f"/manage/{public_id}/headcount", {"headcount": raw})
            assert response.status_code == 303
            assert "Saisissez un nombre entier." in admin.get(f"/manage/{public_id}").text

    def test_edit_details_in_preparation(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        post(admin, f"/manage/{public_id}/details", {"title": "Titre corrigé", "quorum_rule": "50"})
        page = admin.get(f"/manage/{public_id}").text
        assert "Titre corrigé" in page and "Informations enregistrées." in page

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"registered_count": "abc"}, "Saisissez un nombre entier."),
            ({"quorum_rule": "moitié"}, "Indiquez le quorum"),
        ],
    )
    def test_members_and_quorum_set_in_the_details(
        self, admin: TestClient, world: World, owner: User, fields: dict[str, str], message: str
    ) -> None:
        """Left out of the creation form, registered members and quorum are set in the details
        of the session, which refuse a wrong value and keep the previous ones."""
        public_id = world.assembly(owner, in_session=False, title="AG")
        details = f"/manage/{public_id}/details"
        post(admin, details, {"title": "AG", "registered_count": "120", "quorum_rule": "1/4"})
        post(admin, details, {"title": "AG", **fields})
        assert message in admin.get(f"/manage/{public_id}").text
        with world.database.read() as db:
            assembly = db.scalars(select(Assembly).filter_by(public_id=public_id)).one()
            assert (assembly.registered_count, assembly.quorum_rule) == (120, "1/4")


class TestAccessPanel:
    def test_password_lock_headcount(self, admin: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        post(admin, f"/manage/{public_id}/password", {"password": "Assemblée 26"})
        assert world.password(owner, public_id) == "Assemblée 26"
        post(admin, f"/manage/{public_id}/password", {"password": ""})
        assert re.fullmatch(r"\d{8}", world.password(owner, public_id))
        post(admin, f"/manage/{public_id}/password", {"password": "abc"})
        assert "Le mot de passe est trop court." in admin.get(f"/manage/{public_id}").text
        post(admin, f"/manage/{public_id}/entries", {"locked": "1"})
        assert "Déverrouiller les entrées" in admin.get(f"/manage/{public_id}").text
        post(admin, f"/manage/{public_id}/entries", {"locked": "0"})
        world.voters(owner, public_id, 3)
        post(admin, f"/manage/{public_id}/headcount", {"headcount": "2"})
        page = admin.get(f"/manage/{public_id}").text
        assert "3 accès en cours pour 2 personnes comptées" in page
        post(admin, f"/manage/{public_id}/headcount", {"headcount": "-4"})
        assert "Saisissez un nombre entier." in admin.get(f"/manage/{public_id}").text

    @pytest.mark.parametrize("in_session", [True, False])
    def test_password_shown_as_on_the_screen(
        self, admin: TestClient, world: World, owner: User, in_session: bool
    ) -> None:
        """Generated: two groups of four, in the join card of Access and attendance in every
        state, and in the "Join" command of Steering during the session. Chosen by the tribune:
        as typed."""
        public_id = world.assembly(owner, in_session=in_session)

        def secret() -> str:
            page = admin.get(f"/manage/{public_id}").text
            start = page.index('<section class="rubric" id="tab-access"')
            steering = ""  # Steering exists once the session is open
            if in_session:
                steering = page[page.index('<section class="rubric" id="tab-cockpit"') : start]
            found = re.findall(r'<p class="secret">(.*?)</p>', page[start:], re.S)
            assert len(found) == 1  # once in its rubric
            command = re.findall(r'<p class="secret">(.*?)</p>', steering, re.S)
            assert command == (found if in_session else [])  # and in the command
            return found[0]

        password = world.password(owner, public_id)
        assert secret() == f"<span>{password[:4]}</span><span>{password[4:]}</span>"
        post(admin, f"/manage/{public_id}/password", {"password": "Assemblée 26"})
        assert secret() == "Assemblée 26"
        post(admin, f"/manage/{public_id}/password", {"password": ""})  # generated again
        password = world.password(owner, public_id)
        assert re.fullmatch(r"\d{8}", password)
        assert secret() == f"<span>{password[:4]}</span><span>{password[4:]}</span>"

    def test_codes(self, admin: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        page = post(admin, f"/manage/{public_id}/codes", {"count": "3"})
        assert page.status_code == 200
        codes = re.findall(r"[0-9A-Z]{4}-[0-9A-Z]{4}", page.text)
        assert len(set(codes)) == 3 and page.text.count("<svg") == 3
        assert "Imprimez cette page maintenant" in page.text
        world.join(public_id, codes[0])
        console = admin.get(f"/manage/{public_id}").text
        assert "Actif" in console and codes[0] not in console
        with world.database.read() as db:
            batch = access.list_code_batches(db, owner, public_id)[0]
        post(admin, f"/manage/{public_id}/codes/{batch.id}/revoke")
        assert "Codes révoqués" in admin.get(f"/manage/{public_id}").text
        bad = post(admin, f"/manage/{public_id}/codes", {"count": "0"})
        assert bad.status_code == 303

    def test_mode_switch(self, admin: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        post(admin, f"/manage/{public_id}/mode", {"mode": "codes"})
        with world.database.read() as db:
            mode = db.scalar(select(Assembly.access_mode))
        assert mode is AccessMode.CODES
        assert "Gérer les codes" in admin.get(f"/manage/{public_id}").text

    def test_no_new_credentials_while_a_poll_is_open(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        public_id = world.assembly(owner)
        world.poll(owner, public_id)
        post(admin, f"/manage/{public_id}/mode", {"mode": "codes"})
        with world.database.read() as db:
            mode = db.scalar(select(Assembly.access_mode))
        assert mode is AccessMode.PASSWORD
        assert "avant de changer le mode" in admin.get(f"/manage/{public_id}").text

        codes_id = world.assembly(owner, mode=AccessMode.CODES)
        world.poll(owner, codes_id)
        post(admin, f"/manage/{codes_id}/codes", {"count": "5"})
        with world.database.read() as db:
            assert access.list_code_batches(db, owner, codes_id) == []
        assert "avant de générer des codes" in admin.get(f"/manage/{codes_id}").text


class TestPolls:
    """Creation, the method first (FR-31): step 2 of each method sent as the browser would,
    with its defaults (the editor's own tests cover every setting)."""

    @pytest.mark.parametrize(
        ("kind", "fields", "method", "params", "labels"),
        [
            (
                "resolution",
                {"title": "Comptes"},
                "single_choice",
                {"threshold": "majority", "adopt_option": 1},
                ["Pour", "Contre"],
            ),
            (
                "single_choice",
                {"title": "Lieu", "option_label": ["Salle", "", "Parc", " Ferme "]},
                "single_choice",
                {},
                ["Salle", "Parc", "Ferme"],
            ),
            (
                "approval",
                {"title": "CA", "seats": "2", "option_label": ["Alice", "Bruno", "Chloé"]},
                "approval",
                {"seats": 2, "max_choices": 2},
                ["Alice", "Bruno", "Chloé"],
            ),
            (
                "majority_judgment",
                {"title": "Projets", "option_label": ["Jardin", "Fresque"]},
                "majority_judgment",
                {},
                ["Jardin", "Fresque"],
            ),
        ],
    )
    def test_creation_by_method(
        self,
        admin: TestClient,
        world: World,
        owner: User,
        kind: str,
        fields: dict[str, object],
        method: str,
        params: dict[str, int],
        labels: list[str],
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        response = post(admin, f"/manage/{public_id}/polls/new", step_two(kind, **fields))
        assert response.headers["location"] == f"/manage/{public_id}#drafts"
        (poll,) = poll_rows(world)
        assert (poll.method, poll.params, poll.status) == (method, params, PollStatus.DRAFT)
        assert [o.label for o in poll.options] == labels
        assert poll.blank_label == ("Abstention" if kind == "resolution" else None)

    @pytest.mark.parametrize(
        ("kind", "fields", "message"),
        [
            (
                "approval",
                {"title": "CA", "seats": "3", "option_label": ["Alice", "Bruno"]},
                "Le nombre de sièges",
            ),
            (
                "single_choice",
                {"title": "Lieu", "option_label": ["Salle"]},
                "entre 2 et 30 options",
            ),
            ("mystery", {"title": "?"}, "Choisissez un mode de scrutin."),
            ("resolution", {"title": ""}, "Ce champ est obligatoire."),
        ],
    )
    def test_creation_errors(
        self,
        admin: TestClient,
        world: World,
        owner: User,
        kind: str,
        fields: dict[str, object],
        message: str,
    ) -> None:
        public_id = world.assembly(owner)
        response = post(admin, f"/manage/{public_id}/polls/new", step_two(kind, **fields))
        assert response.status_code == 400 and message in response.text
        assert poll_rows(world) == []

    def test_create_and_push_with_timer(self, admin: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        published: list[tuple[str, str, str]] = []
        admin.app.state.hub.publish = lambda channel, event, data="": published.append(  # type: ignore[attr-defined]
            (channel, event, data)
        )
        post(
            admin,
            f"/manage/{public_id}/polls/new",
            step_two("resolution", title="Budget", action="launch", minutes="3"),
        )
        (poll,) = poll_rows(world)
        assert poll.status is PollStatus.OPEN and poll.closes_at is not None
        assert (f"live:{public_id}", "update", "polls") in published
        panel = admin.get(f"/manage/{public_id}/panel").text
        assert "Budget" in panel and "clôture à" in panel and "Clore le vote" in panel

    def test_the_quick_route_is_gone(self, admin: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        data = {"template": "resolution", "title": "Budget", "push": "1"}
        assert post(admin, f"/manage/{public_id}/polls", data).status_code in (404, 405)
        assert poll_rows(world) == []

    def test_full_poll_lifecycle(self, admin: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, open_it=False)
        base = f"/manage/{public_id}/polls/{poll_id}"
        post(admin, f"{base}/open", {"minutes": ""})
        for token in world.voters(owner, public_id, 3):
            world.vote(public_id, token, poll_id, {"choice": 1})
        assert "3 bulletins / 3 présents" in admin.get(f"/manage/{public_id}/panel").text
        post(admin, f"{base}/close")
        page = admin.get(f"/manage/{public_id}").text
        assert (
            "Vote clos\u00a0; résultat calculé." in page and "Adoptée" in page and "SHA-256" in page
        )
        post(admin, f"{base}/publish", {"voters": "1"})
        (poll,) = poll_rows(world)
        assert (poll.shown_to_voters, poll.shown_on_screen) == (True, False)
        post(admin, f"{base}/publish", {})
        assert "Choisissez où afficher le résultat." in admin.get(f"/manage/{public_id}").text
        post(admin, f"{base}/dismiss")
        assert poll_rows(world)[0].shown_to_voters is False
        post(admin, f"{base}/duplicate")
        post(admin, f"{base}/cancel", {"reason": "Énoncé erroné"})
        page = admin.get(f"/manage/{public_id}").text
        assert "Annulé\u00a0: Énoncé erroné" in page
        rows = poll_rows(world)
        assert [p.status for p in rows] == [PollStatus.CANCELLED, PollStatus.DRAFT]
        post(admin, f"/manage/{public_id}/polls/{rows[1].id}/delete")
        assert len(poll_rows(world)) == 1

    def test_poll_of_another_session_is_not_found(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        first = world.assembly(owner)
        second = world.assembly(owner)
        poll_id = world.poll(owner, second)
        response = post(admin, f"/manage/{first}/polls/{poll_id}/close")
        assert response.status_code == 404
        with world.database.read() as db:
            assert db.get(Poll, poll_id).status is PollStatus.OPEN  # type: ignore[union-attr]


def test_session_log_is_translated(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    world.poll(owner, public_id, RESOLUTION)
    page = admin.get(f"/manage/{public_id}").text
    assert "Session ouverte" in page and "Vote lancé" in page


def test_closed_session_page(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id)
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
    post(admin, f"/manage/{public_id}/close")
    page = admin.get(f"/manage/{public_id}").text
    assert "Nouveau vote" not in page and "Supprimer la session" in page
    with world.database.read() as db:
        assert db.scalar(select(Assembly.status)) is AssemblyStatus.CLOSED
