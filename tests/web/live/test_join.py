"""Voter access: session page, join form, credentials, rate limits, leaving (FR-20 to FR-27,
FR-40, FR-46)."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agaura.models import AccessMode, User
from agaura.services import access, assemblies
from agaura.web.limits import JOIN_FAILURES_PER_IP, join_session_key
from agaura.web.live.routes import VOTER_COOKIE
from tests.conftest import World
from tests.web.conftest import log_in, post
from tests.web.live.conftest import assert_outline, join
from tests.web.manage.test_delegates import make_link, open_link

TESTCLIENT_IP_KEY = "join-ip:testclient"


def set_cookies(response: Any) -> list[str]:
    return response.headers.get_list("set-cookie")


def voter_cookie_header(response: Any) -> str:
    return next(c for c in set_cookies(response) if c.startswith(f"{VOTER_COOKIE}="))


class TestSessionPage:
    def test_unknown_or_impossible_ids_are_404(self, client: TestClient) -> None:
        for path in ("/ZZZZ99", "/favicon.ico", "/manage", "/nope/live"):
            response = client.get(path)
            assert response.status_code == 404, path
        assert "Page introuvable" in client.get("/ZZZZ99").text

    def test_other_spellings_redirect_to_the_canonical_address(
        self, client: TestClient, session_id: str
    ) -> None:
        for spelling in (session_id.lower(), f"{session_id[:3]}-{session_id[3:]}"):
            response = client.get(f"/{spelling}")
            assert response.status_code == 303
            assert response.headers["location"] == f"/{session_id}"

    def test_not_started(self, client: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False, title="AG de printemps")
        response = client.get(f"/{public_id}")
        assert response.status_code == 200
        assert "AG de printemps" in response.text
        assert "La session n'a pas encore commencé." in response.text
        assert "data-autojoin" not in response.text and "data-reload-later" in response.text

    def test_closed(self, client: TestClient, world: World, owner: User, session_id: str) -> None:
        join(client, world, owner, session_id)
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, session_id)
        response = client.get(f"/{session_id}")
        assert response.status_code == 200
        assert "La session est terminée." in response.text
        assert "data-autojoin" not in response.text
        # The device that took part keeps its cookie, for the results published to voters
        # (FR-36); it votes no more.
        assert "set-cookie" not in response.headers
        assert client.get(f"/{session_id}/live").status_code == 401

    def test_join_form_in_password_mode(
        self, client: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        response = client.get(f"/{session_id}")
        assert response.status_code == 200
        assert "AG de l&#39;association" in response.text
        assert "Mot de passe de la session" in response.text
        assert f'action="/{session_id}/join"' in response.text and "data-autojoin" in response.text
        assert response.headers["cache-control"] == "no-store"

    def test_join_form_states_the_secret(
        self, client: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        """FR-40a: one line under the form, the same in every mode and every session (the
        neutral form of scanning addresses has it too: test_limits)."""
        codes = world.assembly(owner, mode=AccessMode.CODES, title="AG à codes")
        lines = set()
        for public_id in (session_id, codes):
            page = client.get(f"/{public_id}").text
            found = re.findall(r'<p class="join-promise">(.*?)</p>', page, re.S)
            assert len(found) == 1
            lines.add(found[0])
        assert len(lines) == 1
        line = lines.pop()
        assert 'aria-hidden="true"' in line  # the shield repeats the words
        assert line.endswith(
            "</svg> Vote secret\u00a0: "
            "aucun bulletin n'est relié à une personne ni à son téléphone."
        )

    def test_join_form_in_codes_mode(self, client: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        response = client.get(f"/{public_id}")
        assert "Votre code personnel" in response.text
        assert "Mot de passe de la session" not in response.text
        assert 'autocapitalize="characters"' in response.text  # Crockford letters and digits
        assert "inputmode" not in response.text

    @pytest.mark.parametrize(
        ("password", "numeric", "hint"),
        [
            (None, True, "Affiché dans la salle\u00a0: 8 chiffres."),  # generated (D10)
            ("1234 5678", True, "Affiché dans la salle\u00a0: 8 chiffres."),  # spaces never count
            ("12345678901", True, "Affiché dans la salle. Vous pouvez"),  # length not told
            ("Tournesol-42", False, "Affiché dans la salle. Vous pouvez"),
            ("1234-5678", False, "Affiché dans la salle. Vous pouvez"),
        ],
        ids=["generated", "spaced", "longer", "letters", "dash"],
    )
    def test_password_field_keyboard(
        self,
        client: TestClient,
        world: World,
        owner: User,
        session_id: str,
        password: str | None,
        numeric: bool,
        hint: str,
    ) -> None:
        """A digit keypad only when the password is digits only: the page tells nothing more,
        and the length only for the generated shape."""
        if password is not None:
            with world.database.write() as db:
                access.set_session_password(db, world.keyring, owner, session_id, password)
        response = client.get(f"/{session_id}")
        field = re.search(r'<input id="field-secret"[^>]*>', response.text)
        assert field is not None
        assert ('inputmode="numeric"' in field[0]) is numeric
        assert 'type="text"' in field[0] and "pattern" not in field[0]  # spaces, paste: as today
        assert 'autocapitalize="none"' in field[0]
        assert hint in response.text
        if "8 chiffres" not in hint:
            assert "chiffres" not in response.text
        for secret in {world.password(owner, session_id), password or ""} - {""}:
            assert secret not in response.text

    def test_headings(self, client: TestClient, world: World, owner: User) -> None:
        """The session title is the level-one heading of the join and status pages too."""
        for in_session in (True, False):
            public_id = world.assembly(owner, in_session=in_session, title="AG d'été")
            found = assert_outline(client.get(f"/{public_id}").text, "AG d&#39;été")
            assert (
                2,
                "Rejoindre la session" if in_session else "La session n'a pas encore commencé.",
            ) in found


class TestJoin:
    def test_password_with_forgiving_input(
        self, client: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        password = world.password(owner, session_id)
        typed = f" {password[:3]} - {password[3:]} "
        response = post(client, f"/{session_id}/join", {"secret": typed})
        assert response.status_code == 303 and response.headers["location"] == f"/{session_id}"
        cookie = voter_cookie_header(response)
        assert f"Path=/{session_id}" in cookie and "HttpOnly" in cookie
        assert "Max-Age=86400" in cookie
        page = client.get(f"/{session_id}")
        assert page.status_code == 200 and "En attente du prochain vote" in page.text
        with world.database.read() as db:
            assert access.attendees(db, assemblies.find_assembly(db, session_id)) == 1

    def test_individual_code(self, client: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        code = world.codes(owner, public_id, 1)[0]
        response = post(client, f"/{public_id}/join", {"secret": code.replace("-", " ").lower()})
        assert response.status_code == 303
        assert "En attente du prochain vote" in client.get(f"/{public_id}").text

    def test_wrong_secret(self, client: TestClient, app: FastAPI, session_id: str) -> None:
        response = post(client, f"/{session_id}/join", {"secret": "nope"})
        assert response.status_code == 400
        assert '<div class="alert alert-error" role="alert" id="page-error">' in response.text
        assert "Mot de passe ou code incorrect." in response.text
        # The field says it was refused and why, and takes the focus back.
        field = re.search(r'<input id="field-secret"[^>]*>', response.text)
        assert field is not None
        assert 'aria-describedby="field-secret-hint page-error"' in field[0]
        assert 'aria-invalid="true" autofocus' in field[0]
        assert VOTER_COOKIE not in client.cookies
        limiter = app.state.limiter
        assert limiter.count(TESTCLIENT_IP_KEY, 600) == 1
        assert limiter.count(join_session_key(session_id), 600) == 1

    def test_entries_locked(
        self, client: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        with world.database.write() as db:
            access.set_entries_locked(db, owner, session_id, True)
        response = post(
            client, f"/{session_id}/join", {"secret": world.password(owner, session_id)}
        )
        assert response.status_code == 400 and "Les entrées sont fermées" in response.text

    def test_rate_limit_per_address(
        self, client: TestClient, app: FastAPI, world: World, owner: User, session_id: str
    ) -> None:
        for _ in range(JOIN_FAILURES_PER_IP[0]):
            app.state.limiter.record(TESTCLIENT_IP_KEY)
        response = post(
            client, f"/{session_id}/join", {"secret": world.password(owner, session_id)}
        )
        assert response.status_code == 429 and "Trop de tentatives" in response.text
        assert VOTER_COOKIE not in client.cookies

    def test_join_is_refused_outside_the_session(
        self, client: TestClient, world: World, owner: User
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        response = post(client, f"/{public_id}/join", {"secret": world.password(owner, public_id)})
        assert response.status_code == 409 and "pas encore commencé" in response.text
        assert VOTER_COOKIE not in client.cookies

    def test_join_requires_csrf(self, client: TestClient, session_id: str) -> None:
        client.get("/")
        assert client.post(f"/{session_id}/join", data={"secret": "x"}).status_code == 403

    def test_a_second_scan_reuses_the_credential(
        self, client: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        join(client, world, owner, session_id)
        join(client, world, owner, session_id)
        with world.database.read() as db:
            assert access.attendees(db, assemblies.find_assembly(db, session_id)) == 1

    def test_join_publishes_turnout(
        self,
        client: TestClient,
        app: FastAPI,
        world: World,
        owner: User,
        session_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        published: list[tuple[str, ...]] = []
        monkeypatch.setattr(app.state.hub, "publish", lambda *args: published.append(args))
        join(client, world, owner, session_id)
        assert published == [(f"manage:{session_id}", "turnout")]


class TestAccessCap:
    """Password mode stops issuing credentials at the access cap (FR-27); see
    ``tests/services/test_access.py`` for the cap itself."""

    def test_a_new_device_is_refused_at_the_cap(
        self, client: TestClient, app: FastAPI, world: World, owner: User
    ) -> None:
        public_id = world.assembly(owner, registered_count=1)  # cap: 1 + 10
        world.voters(owner, public_id, 11)
        response = post(client, f"/{public_id}/join", {"secret": world.password(owner, public_id)})
        assert response.status_code == 409
        assert "Cette session a atteint son nombre maximal d&#39;accès" in response.text
        assert "Adressez-vous à la tribune." in response.text
        assert "data-autojoin" in response.text and VOTER_COOKIE not in client.cookies
        # The right password: never counted as a failure, so a room behind one address that
        # meets the cap is not locked out once the tribune raises it.
        limiter = app.state.limiter
        assert limiter.count(TESTCLIENT_IP_KEY, 600) == 0
        assert limiter.count(join_session_key(public_id), 600) == 0
        wrong = post(client, f"/{public_id}/join", {"secret": "00000000"})
        assert wrong.status_code == 400 and "Mot de passe ou code incorrect." in wrong.text

    def test_the_status_bar_warns_until_the_headcount_raises_the_cap(
        self, client: TestClient, app: FastAPI, world: World, owner: User
    ) -> None:
        public_id = world.assembly(owner, registered_count=1)
        tribune = TestClient(app, follow_redirects=False)
        log_in(tribune, owner.email)
        _, key = make_link(world, owner, public_id, ["timer"])
        delegate = open_link(app, public_id, key)
        panel_url = f"/manage/{public_id}/panel"
        world.voters(owner, public_id, 10)
        assert 'id="access-cap"' not in tribune.get(panel_url).text

        join(client, world, owner, public_id)  # the eleventh device: the console refreshes
        for browser in (tribune, delegate):
            panel = browser.get(panel_url).text
            assert 'id="access-cap"' in panel and "Maximum de 11 accès atteint" in panel
        assert 'id="access-cap"' in tribune.get(f"/manage/{public_id}").text

        response = post(tribune, f"/manage/{public_id}/headcount", {"headcount": "30"})
        assert response.status_code == 303
        assert 'id="access-cap"' not in tribune.get(panel_url).text
        other = TestClient(app, follow_redirects=False)
        join(other, world, owner, public_id)

    def test_codes_mode_never_shows_the_warning(
        self, client: TestClient, app: FastAPI, world: World, owner: User
    ) -> None:
        public_id = world.assembly(owner, registered_count=1)
        world.voters(owner, public_id, 11)
        tribune = TestClient(app, follow_redirects=False)
        log_in(tribune, owner.email)
        with world.database.write() as db:
            access.set_access_mode(db, owner, public_id, AccessMode.CODES)
        assert 'id="access-cap"' not in tribune.get(f"/manage/{public_id}/panel").text


class TestCredential:
    def test_stale_cookie_is_deleted(self, client: TestClient, session_id: str) -> None:
        client.cookies.set(VOTER_COOKIE, "forged", domain="testserver.local", path=f"/{session_id}")
        response = client.get(f"/{session_id}")
        assert response.status_code == 200
        assert "plus valable" in response.text and "data-autojoin" in response.text
        assert voter_cookie_header(response).startswith(f'{VOTER_COOKIE}=""')

    def test_switching_to_codes_mode_disables_devices(
        self, client: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        join(client, world, owner, session_id)
        with world.database.write() as db:
            access.set_access_mode(db, owner, session_id, AccessMode.CODES)
        assert client.get(f"/{session_id}/live").status_code == 401
        page = client.get(f"/{session_id}")
        assert "Votre code personnel" in page.text and "plus valable" in page.text

    def test_leave(self, client: TestClient, world: World, owner: User, session_id: str) -> None:
        join(client, world, owner, session_id)
        response = post(client, f"/{session_id}/leave")
        assert response.status_code == 303 and response.headers["location"] == f"/{session_id}"
        cookie = voter_cookie_header(response)
        assert f"Path=/{session_id}" in cookie and "Max-Age=0" in cookie
        page = client.get(f"/{session_id}")
        assert "Vous avez quitté la session sur cet appareil." in page.text
        assert "data-autojoin" in page.text
        assert client.get(f"/{session_id}/live").status_code == 401

    def test_leave_frees_the_code_for_another_device(
        self, client: TestClient, other_client: TestClient, world: World, owner: User
    ) -> None:
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        code = world.codes(owner, public_id, 1)[0]
        assert post(client, f"/{public_id}/join", {"secret": code}).status_code == 303
        assert post(client, f"/{public_id}/leave").status_code == 303
        assert post(other_client, f"/{public_id}/join", {"secret": code}).status_code == 303
        assert "En attente du prochain vote" in other_client.get(f"/{public_id}").text
