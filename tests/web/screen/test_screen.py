"""Projection screen (FR-70 to FR-74, FR-27): access through the secret link, content per state,
what never appears on it (results above all, until the tribune shows one: test_results.py)."""

from __future__ import annotations

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient
from markupsafe import Markup

from agaura.models import AccessMode, Poll, User, utcnow
from agaura.services import assemblies
from agaura.services import screen as screen_service
from agaura.web.live.routes import VOTER_COOKIE
from agaura.web.screen import routes
from agaura.web.security import CSRF_COOKIE
from tests.conftest import RESOLUTION, World
from tests.web.conftest import post
from tests.web.screen.conftest import ICON, access_action, poll_action, screen_url, titled

# Texts of a result (macros/results.html, screen/macros.html) that the screen never shows
# until the tribune shows that result (FR-72, FR-75).
RESULT_TEXTS = [
    "Adoptée",
    "Rejetée",
    "Résultat\u00a0:",
    "Élus",
    "Égalité",
    "Mention majoritaire",
    "Bulletins déposés",
    "Suffrages exprimés",
    "Blancs",
    " %",
    'class="result',
    "verdict",
    "merit",
]


def configure(world: World, public_id: str, **fields: object) -> None:
    """Set columns of the session directly (agenda, chronometer, entries)."""
    with world.database.write() as db:
        assembly = assemblies.find_assembly(db, public_id)
        assert assembly is not None
        for name, value in fields.items():
            setattr(assembly, name, value)


@pytest.fixture
def qr_urls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """URLs encoded in the QR codes of the screen (the SVG itself cannot be read back)."""
    urls: list[str] = []

    def fake_qr(url: str) -> Markup:
        urls.append(url)
        return Markup('<svg class="fake-qr"></svg>')

    monkeypatch.setattr(routes, "qr_svg", fake_qr)
    return urls


def strict_page(html: str) -> None:
    """Strict CSP: no inline script, no style attribute, no external resource."""
    assert "<style" not in html
    assert not re.search(r"\sstyle=", html)
    assert not re.search(r"<script(?![^>]*\ssrc=\"/static/)", html)
    assert not re.search(r"(src|href)=\"(https?:)?//", html)


# --- Access -----------------------------------------------------------------------------------


class TestAccess:
    def test_valid_link(self, client: TestClient, url: str) -> None:
        response = client.get(url)
        assert response.status_code == 200
        assert "AG de l&#39;association" in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "noindex" in response.headers["x-robots-tag"]
        assert "default-src 'self'" in response.headers["content-security-policy"]
        strict_page(response.text)
        # Every visible part sits in a landmark (axe "region"): the mark of AGaura has its own.
        assert '<aside class="screen-brand" aria-label="AGaura">' in response.text

    def test_wrong_token(self, client: TestClient, session_id: str, url: str) -> None:
        for path in ("", "/state", "/events"):
            response = client.get(f"/{session_id}/screen/not-the-token{path}")
            assert response.status_code == 404, path
            assert "Page introuvable" in response.text
        assert client.get(url[:-1] + ("A" if url[-1] != "A" else "B")).status_code == 404

    def test_token_of_another_session(
        self, client: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        other = world.assembly(owner, title="Autre AG")
        other_token = screen_url(world, owner, other).rsplit("/", 1)[1]
        for path in ("", "/state", "/events"):
            assert client.get(f"/{session_id}/screen/{other_token}{path}").status_code == 404

    def test_unknown_or_deleted_session(
        self, client: TestClient, world: World, owner: User
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        url = screen_url(world, owner, public_id)
        assert client.get(url).status_code == 200
        with world.database.write() as db:
            assemblies.delete_assembly(db, owner, public_id)
        for path in ("", "/state", "/events"):
            assert client.get(url + path).status_code == 404
        token = url.rsplit("/", 1)[1]
        assert client.get(f"/nope/screen/{token}").status_code == 404
        assert client.get(f"/ZZZZ99/screen/{token}/state").status_code == 404

    def test_regenerated_link_stops_working(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        access_action(world, owner, "regenerate_screen_token", world.keyring, owner, session_id)
        for path in ("", "/state", "/events"):
            assert client.get(url + path).status_code == 404
        assert client.get(screen_url(world, owner, session_id)).status_code == 200

    def test_non_canonical_session_id(self, client: TestClient, session_id: str, url: str) -> None:
        token = url.rsplit("/", 1)[1]
        response = client.get(f"/{session_id.lower()}/screen/{token}")
        assert response.status_code == 303
        assert response.headers["location"] == url
        assert response.headers["cache-control"] == "no-store"
        # The script's requests use the canonical address, but other spellings work too.
        assert client.get(f"/{session_id.lower()}/screen/{token}/state").status_code == 200

    def test_no_account_needed_and_nothing_secret_leaks(self, client: TestClient, url: str) -> None:
        response = client.get(url)
        cookies = response.headers.get_list("set-cookie")
        assert all(cookie.startswith(f"{CSRF_COOKIE}=") for cookie in cookies)
        assert client.cookies[CSRF_COOKIE] not in response.text  # no form, no CSRF token
        assert "<form" not in response.text


# --- Content ----------------------------------------------------------------------------------


class TestJoinPanel:
    def test_password_mode_in_session(
        self,
        client: TestClient,
        world: World,
        owner: User,
        session_id: str,
        url: str,
        qr_urls: list[str],
    ) -> None:
        world.voters(owner, session_id, 3)
        password = world.password(owner, session_id)
        page = client.get(url).text
        assert f"<span>{password[:4]}</span><span>{password[4:]}</span>" in page
        assert 'class="fake-qr"' in page
        assert qr_urls == [f"http://testserver/{session_id}#k={password}"]
        assert f"testserver/<strong>{session_id}</strong>" in page
        assert "screen-join is-large" in page  # on the stage: the hint on the QR code too
        assert re.search(rf'id="screen-join-label">{ICON}Pour voter</p>', page)
        assert '<p class="join-label">Mot de passe</p>' in page
        assert "Ou scannez le QR code\u00a0: il vous connecte directement." in page
        assert re.search(r"Présents</dt><dd[^>]*>3</dd>", page)
        assert 'Quorum</dt><dd>25 <span class="quorum-state">non atteint' in page
        assert "La session va bientôt commencer." not in page

    def test_password_is_url_quoted_in_the_qr_code(
        self,
        client: TestClient,
        world: World,
        owner: User,
        session_id: str,
        url: str,
        qr_urls: list[str],
    ) -> None:
        access_action(
            world, owner, "set_session_password", world.keyring, owner, session_id, "été #2026"
        )
        page = client.get(url).text
        assert qr_urls == [f"http://testserver/{session_id}#k=%C3%A9t%C3%A9%20%232026"]
        assert "été #2026" in page

    def test_quorum_reached(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        world.voters(owner, session_id, 25)
        page = client.get(url).text
        assert 'class="stat quorum reached"' in page and "atteint" in page

    def test_preparation_shows_how_to_join_ahead(
        self, client: TestClient, world: World, owner: User, qr_urls: list[str]
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        page = client.get(screen_url(world, owner, public_id)).text
        assert "La session va bientôt commencer." in page
        assert "votre téléphone rejoindra la session dès son ouverture" in page
        password = world.password(owner, public_id)
        assert f"<span>{password[:4]}</span><span>{password[4:]}</span>" in page
        assert len(qr_urls) == 1
        assert "Présents" not in page  # nobody can have joined yet

    @pytest.mark.parametrize("in_session", [True, False])
    def test_locked_entries_hide_password_and_qr(
        self,
        client: TestClient,
        world: World,
        owner: User,
        qr_urls: list[str],
        in_session: bool,
    ) -> None:
        public_id = world.assembly(owner, in_session=in_session)
        password = "zèbre mauve"  # a password that cannot appear by chance in a token
        access_action(
            world, owner, "set_session_password", world.keyring, owner, public_id, password
        )
        access_action(world, owner, "set_entries_locked", owner, public_id, True)
        url = screen_url(world, owner, public_id)
        for page in (client.get(url).text, client.get(f"{url}/state").text):
            assert password not in page
            assert "data-password" not in page
            assert 'class="join-qr"' not in page
            assert f"Adresse\u00a0: <strong>http://testserver/{public_id}</strong>" in page
        assert qr_urls == []
        access_action(world, owner, "set_entries_locked", owner, public_id, False)
        assert password in client.get(url).text

    def test_codes_mode(self, client: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        codes = world.codes(owner, public_id, 2)
        world.join(public_id, codes[0])
        password = world.password(owner, public_id)
        page = client.get(screen_url(world, owner, public_id)).text
        assert "Puis saisissez le code imprimé sur votre carte" in page
        assert f"testserver/<strong>{public_id}</strong>" in page
        assert password not in page
        assert 'class="join-qr"' not in page and "data-password" not in page
        assert all(code not in page for code in codes)
        assert re.search(r"Présents</dt><dd[^>]*>1</dd>", page)

    def test_closed_session(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        password = world.password(owner, session_id)
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, session_id)
        page = client.get(url).text
        assert "La session est terminée." in page and "Merci de votre participation." in page
        assert password not in page and 'class="join-qr"' not in page
        assert "Adresse" not in page and "Présents" not in page


class TestPolls:
    def test_open_polls_with_turnout(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        voters = world.voters(owner, session_id, 3)
        first = world.poll(owner, session_id)
        world.poll(owner, session_id, titled("Second vote", "Rouge", "Bleu", "Vert"))
        world.vote(session_id, voters[0], first, {"choice": 1})
        world.vote(session_id, voters[1], first, None)
        page = client.get(url).text
        # Most recently pushed first.
        assert page.index("Second vote") < page.index("Approbation des comptes")
        assert "Vote en cours" in page
        for label in ("Pour", "Contre", "Abstention", "Rouge", "Bleu", "Vert"):
            assert f">{label}</li>" in page
        assert "2 bulletins / 3 présents" in page
        assert "0 bulletin / 3 présents" in page
        assert 'max="3" value="2"' in page
        assert "data-closes-at" not in page

    def test_timer(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        poll_id = world.poll(owner, session_id, open_it=False)
        poll_action(world, owner, "open_poll", poll_id, minutes=5)
        with world.database.read() as db:
            closes_at = db.get_one(Poll, poll_id).closes_at
        assert closes_at is not None
        page = client.get(url).text
        assert f'data-closes-at="{int(closes_at.timestamp() * 1000)}"' in page
        assert "Clôture à " in page and "Clôture dans" in page

    def test_poll_whose_timer_ran_out_is_listed_as_closed(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        poll_id = world.poll(owner, session_id)
        with world.database.write() as db:
            db.get_one(Poll, poll_id).closes_at = utcnow() - dt.timedelta(seconds=1)
        page = client.get(url).text
        assert "Vote en cours" not in page and ">Pour</li>" not in page
        assert re.search(rf"Approbation des comptes</span>\s*<span[^>]*>{ICON}Vote clos<", page)

    def test_approval_majority_judgment_and_condorcet_rules(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        from tests.web.live.conftest import APPROVAL, CONDORCET, MAJORITY_JUDGMENT

        world.poll(owner, session_id, APPROVAL)
        world.poll(owner, session_id, MAJORITY_JUDGMENT)
        world.poll(owner, session_id, CONDORCET)
        page = client.get(url).text
        assert "Cochez au plus 2 options." in page
        assert "Évaluez chaque option." in page
        assert "Classez les options par ordre de préférence." in page
        assert 'class="blank"' not in page  # blank is a separate button for these methods

    def test_questions_put_to_the_vote_show_no_result_until_the_tribune_does(
        self, admin: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        """Closed, published, taken off the screen and cancelled polls: title and state only.
        Never a verdict, a count, a share, an option or a reason (FR-72), unless the tribune
        shows the result of a closed poll (FR-75)."""
        from tests.web.live.conftest import APPROVAL, MAJORITY_JUDGMENT

        voters = world.voters(owner, session_id, 4)
        world.poll(owner, session_id, titled("Brouillon préparé"), open_it=False)
        steps = {
            "Clos non publié": [("close_poll", {})],
            "Publié aux votants": [
                ("close_poll", {}),
                ("publish_result", {"voters": True}),
            ],
            "Affiché puis retiré": [
                ("close_poll", {}),
                ("publish_result", {"voters": True}),
                ("show_on_screen", {"shown": True}),
                ("show_on_screen", {"shown": False}),
            ],
            "Publié puis masqué": [
                ("close_poll", {}),
                ("publish_result", {"voters": True}),
                ("dismiss_result", {}),
            ],
            "Annulé en cours": [("cancel_poll", {"reason": "Erreur de libellé"})],
            "Affiché puis annulé": [
                ("close_poll", {}),
                ("publish_result", {"voters": True}),
                ("show_on_screen", {"shown": True}),
                ("cancel_poll", {"reason": "Irrégularité"}),
            ],
        }
        for title, actions in steps.items():
            poll_id = world.poll(owner, session_id, titled(title, "Rouge", "Bleu"))
            for index, token in enumerate(voters):
                world.vote(session_id, token, poll_id, {"choice": 1 + index % 2})
            for action, kwargs in actions:
                poll_action(world, owner, action, poll_id, **kwargs)
        for data, content in (
            (APPROVAL, {"approved": [1, 2]}),
            (MAJORITY_JUDGMENT, {"grades": {"1": 0, "2": 1, "3": 2}}),
        ):
            poll_id = world.poll(owner, session_id, data)
            for token in voters:
                world.vote(session_id, token, poll_id, content)
            poll_action(world, owner, "close_poll", poll_id)
            poll_action(world, owner, "publish_result", poll_id, voters=True)

        console = admin.get(f"/manage/{session_id}").text
        assert "Élus" in console and "Mention majoritaire" in console  # results exist
        for page in (admin.get(url).text, admin.get(f"{url}/state").text):
            for text in RESULT_TEXTS:
                assert text not in page, text
            for label in ("Rouge", "Bleu", "Alice", "Jardin"):
                assert label not in page, label
            assert "Erreur de libellé" not in page and "Irrégularité" not in page
            assert "Brouillon préparé" not in page
            assert "Questions mises aux voix" in page
            # Most recently pushed first, each with its state.
            listed = re.findall(
                rf'<span class="past-title">([^<]*)</span>\s*'
                rf'<span class="past-state">{ICON}([^<]*)<',
                page,
            )
            assert listed[:6] == [
                ("Projets de quartier", "Vote clos"),
                ("Élection du conseil", "Vote clos"),
                ("Affiché puis annulé", "Vote annulé"),
                ("Annulé en cours", "Vote annulé"),
                ("Publié puis masqué", "Vote clos"),
                ("Affiché puis retiré", "Vote clos"),
            ]

    def test_long_history_is_cut(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        for number in range(1, 13):
            poll_id = world.poll(owner, session_id, titled(f"Vote numéro {number}"))
            poll_action(world, owner, "close_poll", poll_id)
        with world.database.write() as db:
            assembly = assemblies.find_assembly(db, session_id)
            assert assembly is not None
            assembly.entries_locked = True  # the history takes the stage
        page = client.get(url).text
        assert "Vote numéro 12" in page and "Vote numéro 3<" in page
        assert "Vote numéro 2<" not in page
        assert "2 votes précédents" in page
        assert "En attente du prochain vote" in page
        world.poll(owner, session_id, titled("Vote ouvert"))
        page = client.get(url).text  # an open poll: three past ones in a strip below it
        assert "Vote numéro 12" in page and "Vote numéro 10" in page
        assert "Vote numéro 9<" not in page and "9 votes précédents" in page

    def test_after_the_session(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        poll_id = world.poll(owner, session_id)
        poll_action(world, owner, "close_poll", poll_id)
        poll_action(world, owner, "publish_result", poll_id, voters=True)
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, session_id)
        page = client.get(url).text
        assert "Approbation des comptes" in page and "Vote clos" in page
        assert "Adoptée" not in page
        assert "La session est terminée." in page and "Merci de votre participation." in page

    def test_nothing_voter_specific(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        """A screen opened on a phone that has joined shows the same page as any screen."""
        response = post(
            client, f"/{session_id}/join", {"secret": world.password(owner, session_id)}
        )
        assert response.status_code == 303
        voter_token = client.cookies[VOTER_COOKIE]
        poll_id = world.poll(owner, session_id)
        world.vote(session_id, voter_token, poll_id, {"choice": 1})
        response = client.get(url)
        assert voter_token not in response.text
        assert "Vote enregistré" not in response.text and "voted" not in response.text
        assert "1 bulletin / 1 présent" in response.text
        assert not any(VOTER_COOKIE in cookie for cookie in response.headers.get_list("set-cookie"))


class TestEntriesStatus:
    def entries(self, client: TestClient, url: str) -> tuple[str, str]:
        match = re.search(rf'data-entries="(\w+)">{ICON}([^<]*)<', client.get(url).text)
        assert match is not None
        return match.group(1), match.group(2)

    def test_every_state(self, client: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False)
        url = screen_url(world, owner, public_id)
        assert self.entries(client, url) == ("soon", "Ouverture prochaine")
        with world.database.write() as db:
            assemblies.open_assembly(db, owner, public_id)
        assert self.entries(client, url) == ("open", "Entrées ouvertes")
        access_action(world, owner, "set_entries_locked", owner, public_id, True)
        assert self.entries(client, url) == ("locked", "Entrées fermées")
        access_action(world, owner, "set_entries_locked", owner, public_id, False)
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, public_id)
        assert self.entries(client, url) == ("ended", "Session terminée")

    def test_locked_before_the_opening(self, client: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False)
        access_action(world, owner, "set_entries_locked", owner, public_id, True)
        url = screen_url(world, owner, public_id)
        assert self.entries(client, url) == ("locked", "Entrées fermées")
        page = client.get(url).text
        assert "data-password" not in page and "La session va bientôt commencer." in page


class TestAgenda:
    TITLES = tuple(f"Point {number}" for number in range(1, 13))

    def items(self, page: str) -> list[tuple[str, str, str]]:
        """(number, title, class) of each agenda line shown."""
        return re.findall(
            r'<li class="(\w*)"[^>]*>\s*<span class="agenda-number">(\d+)</span>\s*'
            r'<span class="agenda-title">(?:<span class="agenda-now">En cours</span> )?'
            r"([^<]*)</span>",
            page,
        )

    def test_window_around_the_current_item(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        configure(
            world,
            session_id,
            agenda=[{"title": title} for title in self.TITLES],
            agenda_current=6,
            entries_locked=True,  # the agenda takes the stage: a window of 9 items
        )
        page = client.get(url).text
        assert "Ordre du jour" in page
        # From the item before the current one, slid back to fill the window near the end.
        assert self.items(page) == [
            ("past", "4", "Point 4"),
            ("past", "5", "Point 5"),
            ("current", "6", "Point 6"),
            *[("", str(number), f"Point {number}") for number in range(7, 13)],
        ]
        assert '<li class="current" aria-current="step">' in page
        assert "En cours" in page
        assert "3 points précédents" in page and "suivant" not in page
        configure(world, session_id, agenda_current=2)
        page = client.get(url).text
        assert [number for _, number, _ in self.items(page)] == [str(n) for n in range(1, 10)]
        assert "précédent" not in page and "3 points suivants" in page

    def test_side_column_and_end_of_the_list(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        configure(
            world,
            session_id,
            agenda=[{"title": title} for title in self.TITLES],
            agenda_current=12,
            timer_shown=True,
        )
        # Join panel on stage; chronometer and agenda in the side column: 5 items.
        page = client.get(url).text
        assert [number for _, number, _ in self.items(page)] == ["8", "9", "10", "11", "12"]
        assert "7 points précédents" in page and "suivant" not in page

    def test_no_current_item(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        configure(world, session_id, agenda=[{"title": "Rapport moral"}, {"title": "Budget"}])
        page = client.get(url).text
        assert self.items(page) == [("", "1", "Rapport moral"), ("", "2", "Budget")]
        assert "aria-current" not in page and "précédent" not in page

    def test_stale_position_highlights_nothing(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        configure(world, session_id, agenda=[{"title": "Seul point"}], agenda_current=3)
        assert self.items(client.get(url).text) == [("", "1", "Seul point")]

    def test_empty_agenda_is_not_shown(self, client: TestClient, url: str) -> None:
        assert "Ordre du jour" not in client.get(url).text

    def test_hidden_after_the_session(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        configure(world, session_id, agenda=[{"title": "Rapport moral"}], agenda_current=1)
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, session_id)
        assert "Rapport moral" not in client.get(url).text

    @pytest.mark.parametrize(
        ("current", "size", "shown", "before", "after", "highlighted"),
        [
            (None, 3, [1, 2, 3], 0, 7, None),
            (1, 3, [1, 2, 3], 0, 7, 1),
            (2, 3, [1, 2, 3], 0, 7, 2),
            (5, 3, [4, 5, 6], 3, 4, 5),
            (10, 3, [8, 9, 10], 7, 0, 10),
            (5, 20, list(range(1, 11)), 0, 0, 5),
            (0, 3, [1, 2, 3], 0, 7, None),  # stale positions highlight nothing
            (11, 3, [1, 2, 3], 0, 7, None),
        ],
    )
    def test_window_function(
        self,
        current: int | None,
        size: int,
        shown: list[int],
        before: int,
        after: int,
        highlighted: int | None,
    ) -> None:
        titles = [f"T{number}" for number in range(1, 11)]
        window = screen_service.agenda_window(titles, current, size)
        assert [line.position for line in window.lines] == shown
        assert [line.title for line in window.lines] == [f"T{number}" for number in shown]
        assert (window.before, window.after) == (before, after)
        assert [line.position for line in window.lines if line.current] == (
            [] if highlighted is None else [highlighted]
        )
        assert [line.position for line in window.lines if line.past] == [
            number for number in shown if highlighted is not None and number < highlighted
        ]


class TestChronometer:
    NOW = dt.datetime(2026, 3, 14, 18, 0, tzinfo=dt.UTC)

    @pytest.fixture(autouse=True)
    def frozen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(screen_service, "utcnow", lambda: self.NOW)

    def face(self, client: TestClient, url: str) -> dict[str, str]:
        page = client.get(f"{url}/state").text
        match = re.search(
            r'<section class="screen-timer ([^"]*)"[^>]*data-timer-duration="(\d*)" '
            r'data-timer-elapsed="(\d+)" data-timer-started="(\d*)">\s*'
            rf'<p class="timer-label"[^>]*>{ICON}([^<]*)</p>\s*'
            r'<p class="timer-clock" data-timer-clock>([^<]*)</p>',
            page,
        )
        assert match is not None, page
        classes, duration, elapsed, started, label, clock = match.groups()
        return {
            "classes": classes,
            "duration": duration,
            "elapsed": elapsed,
            "started": started,
            "label": label,
            "clock": clock,
            "page": page,
        }

    def ago(self, seconds: float) -> dt.datetime:
        return self.NOW - dt.timedelta(seconds=seconds)

    def test_hidden_by_default(self, client: TestClient, url: str) -> None:
        assert "data-timer" not in client.get(url).text

    def test_countdown(self, client: TestClient, world: World, session_id: str, url: str) -> None:
        configure(
            world,
            session_id,
            timer_shown=True,
            timer_label="Temps de parole",
            timer_duration=180,
            timer_started_at=self.ago(47.5),
        )
        face = self.face(client, url)
        assert face["clock"] == "2:13"  # 132.5 s left, rounded up
        assert face["label"] == "Temps de parole"
        assert "is-running" in face["classes"]
        assert (face["duration"], face["elapsed"]) == ("180", "0")
        assert face["started"] == str(int(self.ago(47.5).timestamp() * 1000))
        assert "is-overtime" not in face["classes"]
        assert re.search(r"<span data-timer-over hidden>Temps écoulé</span>", face["page"])

    def test_overtime(self, client: TestClient, world: World, session_id: str, url: str) -> None:
        configure(
            world,
            session_id,
            timer_shown=True,
            timer_duration=60,
            timer_elapsed=20,
            timer_started_at=self.ago(55.2),
        )
        face = self.face(client, url)
        assert face["clock"] == "+0:15"
        assert "is-overtime" in face["classes"] and "is-running" in face["classes"]
        assert face["label"] == "Compte à rebours"  # no label typed: the mode
        assert "<span data-timer-over>Temps écoulé</span>" in face["page"]

    def test_paused(self, client: TestClient, world: World, session_id: str, url: str) -> None:
        configure(world, session_id, timer_shown=True, timer_duration=300, timer_elapsed=75)
        face = self.face(client, url)
        assert (face["clock"], face["started"]) == ("3:45", "")
        assert "is-paused" in face["classes"]
        assert "<span>En pause</span>" in face["page"]

    def test_ready(self, client: TestClient, world: World, session_id: str, url: str) -> None:
        configure(world, session_id, timer_shown=True, timer_duration=120)
        face = self.face(client, url)
        assert (face["clock"], face["started"]) == ("2:00", "")
        assert "is-ready" in face["classes"] and "En pause" not in face["page"]

    def test_counting_up(self, client: TestClient, world: World, session_id: str, url: str) -> None:
        configure(
            world,
            session_id,
            timer_shown=True,
            timer_duration=None,
            timer_elapsed=3600,
            timer_started_at=self.ago(61.9),
        )
        face = self.face(client, url)
        assert (face["clock"], face["duration"], face["label"]) == ("1:01:01", "", "Chronomètre")
        assert "is-overtime" not in face["classes"]

    def test_takes_the_stage_when_nothing_else(
        self, client: TestClient, world: World, session_id: str, url: str
    ) -> None:
        configure(world, session_id, timer_shown=True, timer_duration=60, entries_locked=True)
        assert "is-large" in self.face(client, url)["classes"]
        configure(world, session_id, entries_locked=False)  # the join panel takes the stage
        assert "is-large" not in self.face(client, url)["classes"]

    def test_never_after_the_session(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        configure(world, session_id, timer_shown=True, timer_duration=60)
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, session_id)
        assert "data-timer" not in client.get(url).text

    def test_shown_before_the_opening(self, client: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False)
        configure(
            world,
            public_id,
            timer_shown=True,
            timer_label="Début dans",
            timer_duration=600,
            timer_started_at=self.ago(30),
        )
        face = self.face(client, screen_url(world, owner, public_id))
        assert (face["label"], face["clock"]) == ("Début dans", "9:30")


class TestLayout:
    """Which block takes the stage, which go to the side column (``ScreenView``)."""

    def view(self, world: World, public_id: str) -> screen_service.ScreenView:
        with world.database.read() as db:
            assembly = assemblies.find_assembly(db, public_id)
            assert assembly is not None
            return screen_service.screen_view(db, world.keyring, assembly)

    def layout(self, world: World, public_id: str) -> tuple[str, list[str]]:
        view = self.view(world, public_id)
        return view.hero.value, [part.value for part in view.side]

    def test_combinations(self, world: World, owner: User, session_id: str) -> None:
        assert self.layout(world, session_id) == ("join", [])
        configure(world, session_id, agenda=[{"title": "Budget"}])
        assert self.layout(world, session_id) == ("join", ["agenda"])
        configure(world, session_id, timer_shown=True)
        assert self.layout(world, session_id) == ("join", ["timer", "agenda"])
        poll_id = world.poll(owner, session_id)
        assert self.layout(world, session_id) == ("questions", ["timer", "join", "agenda"])
        poll_action(world, owner, "close_poll", poll_id)
        assert self.layout(world, session_id) == ("join", ["timer", "agenda", "questions"])
        configure(world, session_id, entries_locked=True)
        assert self.layout(world, session_id) == ("timer", ["agenda", "questions"])
        configure(world, session_id, timer_shown=False)
        assert self.layout(world, session_id) == ("agenda", ["questions"])
        configure(world, session_id, agenda=[])
        assert self.layout(world, session_id) == ("questions", [])
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, session_id)
        assert self.layout(world, session_id) == ("message", ["questions"])

    def test_waiting(self, world: World, owner: User, session_id: str) -> None:
        configure(world, session_id, entries_locked=True)
        assert self.layout(world, session_id) == ("message", [])
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, session_id)
        assert self.layout(world, session_id) == ("message", [])

    def test_rendered_blocks(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        configure(world, session_id, agenda=[{"title": "Budget"}], timer_shown=True)
        world.poll(owner, session_id)
        page = client.get(url).text
        assert 'class="screen-stage hero-questions has-side"' in page
        side = page[page.index('class="screen-side"') :]
        assert side.index("screen-timer") < side.index("screen-join") < side.index("screen-agenda")
        # Beside other blocks, the way to join takes one row, fitted first, without the hint
        # on the QR code; the agenda leaves it out of the blocks counted for its lines.
        assert 'class="screen-join is-compact is-row"' in page
        assert 'data-fit data-fit-min="0" data-fit-first>' in page
        assert "Ou scannez le QR code" not in page
        assert '<p class="join-label">Mot de passe</p>' in page
        assert page.count("data-fit-first") == 1

    def test_way_to_join_alone_in_the_side_column(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        world.poll(owner, session_id)
        page = client.get(url).text
        side = page[page.index('class="screen-side"') :]
        assert 'class="screen-join is-compact"' in side  # QR code above the text
        assert "data-fit-first" not in page
        assert "Ou scannez le QR code\u00a0: il vous connecte directement." in side

    @pytest.mark.parametrize(
        ("timer_shown", "join", "lines"), [(True, True, 5), (True, False, 5), (False, True, 7)]
    )
    def test_agenda_lines_beside_the_way_to_join(
        self,
        client: TestClient,
        world: World,
        owner: User,
        session_id: str,
        url: str,
        timer_shown: bool,
        join: bool,
        lines: int,
    ) -> None:
        """The agenda of the side column shows more lines when the way to join, a row, shares
        the column with it."""
        configure(
            world,
            session_id,
            agenda=[{"title": f"Point {number}"} for number in range(1, 13)],
            agenda_current=6,
            timer_shown=timer_shown,
            entries_locked=not join,
        )
        world.poll(owner, session_id)
        page = client.get(url).text
        agenda = page[page.index('class="screen-agenda') :]
        assert agenda.count('<span class="agenda-number">') == lines


class TestStateFragment:
    def test_content_only(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        world.poll(owner, session_id)
        response = client.get(f"{url}/state")
        assert response.status_code == 200
        assert "<html" not in response.text and "<header" in response.text
        assert "Approbation des comptes" in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert abs(int(response.headers["x-server-time"]) / 1000 - utcnow().timestamp()) < 60
        strict_page(response.text)

    def test_full_page_wraps_the_fragment(self, client: TestClient, url: str) -> None:
        page = client.get(url).text
        assert f'data-state-url="{url}/state"' in page
        assert f'data-events-url="{url}/events"' in page
        assert re.search(r'data-server-time="\d{13}"', page)
        assert 'src="/static/js/screen.js?v=' in page
        assert 'href="/static/css/screen.css?v=' in page
        assert "data-fullscreen hidden" in page and "Plein écran" in page


def test_screen_view_service(world: World, owner: User, session_id: str) -> None:
    world.voters(owner, session_id, 2)
    poll_id = world.poll(owner, session_id, RESOLUTION)
    with world.database.read() as db:
        assembly = assemblies.find_assembly(db, session_id)
        assert assembly is not None
        view = screen_service.screen_view(db, world.keyring, assembly)
        assert view.in_session and not view.closed and not view.codes_mode
        assert view.entries_open and view.password == world.password(owner, session_id)
        assert (view.attendees, view.quorum, view.quorum_reached) == (2, 25, False)
        assert [(item.poll.id, item.turnout) for item in view.open_polls] == [(poll_id, 0)]
        assert (view.past_polls, view.timer, view.agenda) == ([], None, [])
        assert view.entries is screen_service.Entries.OPEN
        assert view.hero is screen_service.Part.QUESTIONS
        assert view.side == [screen_service.Part.JOIN]
