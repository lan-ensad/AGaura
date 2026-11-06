"""Console side of the projection screen: the chronometer card (FR-74), and the results of closed
polls, published to voters (FR-36) and shown on the screen (FR-75) as two separate choices."""

from __future__ import annotations

import re
from html import unescape

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from agaura.models import Assembly, AuditEntry, Poll, User
from agaura.services import assemblies
from agaura.web.realtime import manage_channel
from tests.conftest import World
from tests.web.conftest import log_in, make_account, post
from tests.web.screen.conftest import poll_action, screen_url


@pytest.fixture
def public_id(world: World, owner: User) -> str:
    return world.assembly(owner)


@pytest.fixture
def published(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    events: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        app.state.hub,
        "publish",
        lambda channel, event, data="": events.append((channel, event, data)),
    )
    return events


def stored(world: World, public_id: str) -> Assembly:
    with world.database.read() as db:
        assembly = assemblies.find_assembly(db, public_id)
        assert assembly is not None
        return assembly


def timer_columns(world: World, public_id: str) -> tuple[object, ...]:
    assembly = stored(world, public_id)
    return (
        assembly.timer_shown,
        assembly.timer_label,
        assembly.timer_duration,
        assembly.timer_elapsed,
        assembly.timer_started_at is not None,
    )


COUNTDOWN_3 = {"mode": "countdown", "duration": "180"}


def start_buttons(page: str) -> int:
    """Buttons that start a chronometer: exactly one, in the setup form, never two."""
    return len(re.findall(r"<button[^>]*>Démarrer</button>", page))


class TestTimerCard:
    def test_setup_form_when_nothing_runs(self, admin: TestClient, public_id: str) -> None:
        page = admin.get(f"/manage/{public_id}").text
        assert '<section class="card timer-card" id="timer"' in page
        assert f'data-timer-url="/manage/{public_id}/timer"' in page
        assert re.search(r'data-server-time="\d{13}"', page)
        assert 'src="/static/js/timer.js?v=' in page
        assert "Chronomètre et compte à rebours" in page and "Non affiché à l'écran" in page
        # Two modes, a countdown proposed first, 3 minutes chosen by default.
        assert '<input type="radio" name="mode" value="countdown" checked>' in page
        assert '<input type="radio" name="mode" value="up">' in page
        assert "Compte à rebours" in page and "d'une durée jusqu'à zéro" in page
        assert "Chronomètre</strong>" in page and "mesure le temps écoulé" in page
        for seconds, label in ((60, "1 min"), (120, "2 min"), (300, "5 min")):
            assert f'name="duration" value="{seconds}"><span>{label}</span>' in page
        assert 'name="duration" value="180" checked><span>3 min</span>' in page
        assert 'name="duration" value="custom"><span>Autre durée</span>' in page
        assert "Libellé (facultatif)" in page and 'name="show" value="1" checked' in page
        assert start_buttons(page) == 1
        # No clock and no control while nothing runs.
        assert "data-timer-clock" not in page and ">Pause<" not in page
        assert ">Réinitialiser<" not in page and "/timer/resume" not in page

    def test_hidden_once_the_session_is_closed(
        self, admin: TestClient, world: World, owner: User, public_id: str
    ) -> None:
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, public_id)
        page = admin.get(f"/manage/{public_id}").text
        assert 'id="timer"' not in page and "timer.js" not in page
        response = post(admin, f"/manage/{public_id}/timer/start", COUNTDOWN_3)
        assert response.status_code == 303
        assert "La session est terminée." in admin.get(f"/manage/{public_id}").text

    def test_start_a_countdown(
        self,
        admin: TestClient,
        world: World,
        owner: User,
        public_id: str,
        published: list[tuple[str, str, str]],
    ) -> None:
        fields = {**COUNTDOWN_3, "label": "Temps de parole", "show": "1", "minutes": "9"}
        response = post(admin, f"/manage/{public_id}/timer/start", fields)
        assert response.status_code == 303
        # No anchor: the card (#timer) and the command of Steering (#cmd-timer) post to their
        # own address, which the browser keeps.
        assert response.headers["location"] == f"/manage/{public_id}"
        # The preset decides; the custom fields count only with "Autre durée".
        assert timer_columns(world, public_id) == (True, "Temps de parole", 180, 0, True)
        # The open screen refreshes on this event (within a second).
        assert published == [(f"manage:{public_id}", "turnout", "console")]
        page = admin.get(f"/manage/{public_id}").text
        assert "Compte à rebours démarré." in page
        assert "Affiché à l'écran" in page and "En marche" in page
        assert "Compte à rebours de 3:00" in page
        assert re.search(r"data-timer-clock>(3:00|2:59)<", page)
        assert ">Pause<" in page and ">+1 min<" in page and ">Réinitialiser<" in page
        assert ">Retirer de l'écran<" in page
        assert start_buttons(page) == 0 and 'name="mode" value="countdown"' not in page
        screen = admin.get(screen_url(world, owner, public_id) + "/state").text
        assert "Temps de parole" in screen and "data-timer-clock" in screen

    def test_start_a_chronometer(
        self, admin: TestClient, world: World, owner: User, public_id: str
    ) -> None:
        # The duration fields are ignored: a chronometer counts the elapsed time.
        post(
            admin,
            f"/manage/{public_id}/timer/start",
            {"mode": "up", "duration": "60", "show": "1"},
        )
        assert timer_columns(world, public_id) == (True, None, None, 0, True)
        page = admin.get(f"/manage/{public_id}").text
        assert "Chronomètre démarré." in page
        assert '<p class="timer-face-label">Chronomètre</p>' in page
        assert ">+1 min<" not in page
        post(admin, f"/manage/{public_id}/timer/add")
        page = admin.get(f"/manage/{public_id}").text
        assert "Seul un compte à rebours a une durée à prolonger." in page
        post(admin, f"/manage/{public_id}/timer/reset")
        page = admin.get(f"/manage/{public_id}").text  # back to setup: the mode used last
        assert '<input type="radio" name="mode" value="up" checked>' in page

    @pytest.mark.parametrize(
        ("fields", "duration"),
        [
            ({"duration": "60"}, 60),
            ({"duration": "300"}, 300),
            ({"duration": "custom", "minutes": "1", "seconds": "30"}, 90),
            ({"duration": "custom", "minutes": "", "seconds": "45"}, 45),
            ({"duration": "custom", "minutes": "20", "seconds": ""}, 1200),
        ],
    )
    def test_durations(
        self,
        admin: TestClient,
        world: World,
        public_id: str,
        fields: dict[str, str],
        duration: int,
    ) -> None:
        post(admin, f"/manage/{public_id}/timer/start", {"mode": "countdown", **fields})
        assert timer_columns(world, public_id) == (False, None, duration, 0, True)

    def test_setup_remembers_the_last_countdown(
        self, admin: TestClient, world: World, public_id: str
    ) -> None:
        base = f"/manage/{public_id}/timer"
        post(
            admin,
            f"{base}/start",
            {"mode": "countdown", "duration": "120", "label": "Débat", "show": "1"},
        )
        post(admin, f"{base}/reset")
        page = admin.get(f"/manage/{public_id}").text
        assert "Réinitialisé\u00a0: préparez le suivant." in page
        assert 'name="mode" value="countdown" checked' in page
        assert 'name="duration" value="120" checked' in page
        assert 'value="Débat"' in page
        # Still on screen, stopped at its full duration: it can be hidden from here.
        assert "L'écran affiche 2:00, à l'arrêt." in page and ">Retirer de l'écran<" in page
        post(
            admin,
            f"{base}/start",
            {"mode": "countdown", "duration": "custom", "minutes": "1", "seconds": "15"},
        )
        post(admin, f"{base}/reset")
        page = admin.get(f"/manage/{public_id}").text
        assert 'name="duration" value="custom" checked' in page
        assert 'class="short" value="1"' in page and 'class="short" value="15"' in page
        assert start_buttons(page) == 1

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"mode": "countdown"}, "Choisissez une durée entre 1 seconde et 10 heures."),
            ({"mode": "countdown", "duration": "custom"}, "Choisissez une durée"),
            (
                {"mode": "countdown", "duration": "custom", "minutes": "0", "seconds": "0"},
                "Choisissez une durée",
            ),
            (
                {"mode": "countdown", "duration": "custom", "minutes": "1", "seconds": "60"},
                "Choisissez une durée",
            ),
            ({"mode": "countdown", "duration": "custom", "minutes": "abc"}, "Choisissez une durée"),
            ({"mode": "countdown", "duration": "custom", "minutes": "²"}, "Choisissez une durée"),
            ({"mode": "countdown", "duration": "custom", "minutes": "601"}, "Choisissez une durée"),
            ({"mode": "countdown", "duration": "-60"}, "Choisissez une durée"),
            ({"mode": "countdown", "duration": "0"}, "Choisissez une durée"),
            ({"duration": "60"}, "Choisissez un compte à rebours ou un chronomètre."),
            (
                {"mode": "later", "duration": "60"},
                "Choisissez un compte à rebours ou un chronomètre.",
            ),
        ],
    )
    def test_invalid_setup(
        self,
        admin: TestClient,
        world: World,
        public_id: str,
        fields: dict[str, str],
        message: str,
    ) -> None:
        post(admin, f"/manage/{public_id}/timer/start", fields)
        assert timer_columns(world, public_id) == (False, None, None, 0, False)
        assert message in admin.get(f"/manage/{public_id}").text

    def test_label_too_long(self, admin: TestClient, world: World, public_id: str) -> None:
        post(admin, f"/manage/{public_id}/timer/start", {**COUNTDOWN_3, "label": "x" * 101})
        assert timer_columns(world, public_id)[4] is False
        assert "Ce texte est trop long." in admin.get(f"/manage/{public_id}").text

    def test_controls(
        self,
        admin: TestClient,
        world: World,
        public_id: str,
        published: list[tuple[str, str, str]],
    ) -> None:
        base = f"/manage/{public_id}/timer"
        post(admin, f"{base}/start", {"mode": "countdown", "duration": "60", "show": "1"})
        steps = [
            # Paused within the first second: one second kept, still paused (not set up).
            ("pause", {}, "En pause\u00a0: le temps est arrêté.", (True, None, 60, 1, False)),
            ("add", {}, "Une minute ajoutée.", (True, None, 120, 1, False)),
            ("resume", {}, "Le temps repart.", (True, None, 120, 1, True)),
            ("show", {"shown": "0"}, "Retiré de l&#39;écran.", (False, None, 120, 1, True)),
            ("show", {"shown": "1"}, "Affiché à l&#39;écran.", (True, None, 120, 1, True)),
            ("reset", {}, "Réinitialisé\u00a0: préparez le suivant.", (True, None, 120, 0, False)),
        ]
        for action, fields, notice, columns in steps:
            response = post(admin, f"{base}/{action}", fields)
            assert response.status_code == 303, action
            assert response.headers["location"] == f"/manage/{public_id}"
            assert timer_columns(world, public_id) == columns, action
            page = admin.get(f"/manage/{public_id}").text
            assert notice in page, action
            assert start_buttons(page) == (1 if action == "reset" else 0), action
        assert len(published) == 1 + len(steps)
        assert set(published) == {(f"manage:{public_id}", "turnout", "console")}

    def test_paused_card(self, admin: TestClient, world: World, public_id: str) -> None:
        post(admin, f"/manage/{public_id}/timer/start", {"mode": "countdown", "duration": "60"})
        with world.database.write() as db:
            assembly = assemblies.find_assembly(db, public_id)
            assert assembly is not None
            assembly.timer_started_at = None
            assembly.timer_elapsed = 75
        page = admin.get(f"/manage/{public_id}").text
        assert 'class="timer-face is-paused is-overtime"' in page
        assert "data-timer-clock>+0:15<" in page and "<span data-timer-over>Temps écoulé" in page
        assert ">Reprendre<" in page and ">Pause<" not in page and "En pause" in page
        assert '<p class="timer-face-label">Compte à rebours de 1:00</p>' in page
        assert start_buttons(page) == 0

    def test_card_alone(self, admin: TestClient, world: World, public_id: str) -> None:
        """For timer.js: the card only, never cached, and the console's notice kept."""
        post(admin, f"/manage/{public_id}/timer/start", {**COUNTDOWN_3, "label": "Débat"})
        response = admin.get(f"/manage/{public_id}/timer")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.text.lstrip().startswith('<section class="card timer-card"')
        assert "data-timer-status" in response.text and "Débat" in response.text
        assert 'name="csrf_token"' in response.text
        assert "set-cookie" not in response.headers  # the flash cookie is left alone
        assert "Compte à rebours démarré." in admin.get(f"/manage/{public_id}").text

    def test_csrf_and_isolation(
        self, client: TestClient, world: World, owner: User, public_id: str
    ) -> None:
        make_account(world, "intruder@example.org")
        log_in(client, "intruder@example.org")
        base = f"/manage/{public_id}/timer"
        assert client.get(base).status_code == 404
        for action in ("start", "pause", "resume", "reset", "add", "show"):
            assert post(client, f"{base}/{action}", COUNTDOWN_3).status_code == 404
        log_in(client, owner.email)
        assert client.post(f"{base}/start", data=COUNTDOWN_3).status_code == 403
        assert timer_columns(world, public_id) == (False, None, None, 0, False)


class TestPublication:
    """Publishing to voters (FR-36) and showing on the projection screen (FR-75): two separate
    buttons of each closed poll, never for a cancelled one."""

    def closed_poll(self, world: World, owner: User, public_id: str, ballots: int) -> int:
        poll_id = world.poll(owner, public_id)
        for token in world.voters(owner, public_id, ballots):
            world.vote(public_id, token, poll_id, {"choice": 1})
        poll_action(world, owner, "close_poll", poll_id)
        return poll_id

    def flags(self, world: World, poll_id: int) -> tuple[bool, bool]:
        with world.database.read() as db:
            poll = db.get_one(Poll, poll_id)
            return poll.shown_to_voters, poll.shown_on_screen

    def test_voters_and_screen_are_separate(
        self,
        admin: TestClient,
        world: World,
        owner: User,
        public_id: str,
        published: list[tuple[str, str, str]],
    ) -> None:
        poll_id = self.closed_poll(world, owner, public_id, 1)
        screen = screen_url(world, owner, public_id)
        page = unescape(admin.get(f"/manage/{public_id}").text)
        assert "Deux gestes indépendants\u00a0: publier, sur les téléphones des votants" in page
        assert "à partir de 10 bulletins, le détail des voix" in page
        assert 'name="voters" value="1"' in page and "Publier le résultat aux votants" in page
        assert 'name="shown" value="1"' in page and "Afficher sur l'écran" in page
        assert "Moins de 10 bulletins\u00a0: l'écran n'affiche que le verdict." in page
        assert "data-on-screen" not in page

        # A form of an older page that still posts ``screen=1`` reaches voters only.
        base = f"/manage/{public_id}/polls/{poll_id}"
        post(admin, f"{base}/publish", {"voters": "1", "screen": "1"})
        assert self.flags(world, poll_id) == (True, False)
        page = unescape(admin.get(f"/manage/{public_id}").text)
        assert "Résultat publié." in page and "Masquer le résultat" in page
        assert "Publier le résultat aux votants" not in page
        view = admin.get(screen).text
        assert "Adoptée" not in view and "Vote clos" in view

        # Shown on the screen: the screen is told at once, the voter pages are not.
        published.clear()
        response = post(admin, f"{base}/screen", {"shown": "1"})
        assert response.status_code == 303
        # No anchor: the finished poll posts to #poll-<id>, the "Vote clos" command of
        # Steering to #tab-cockpit (D-K).
        assert response.headers["location"] == f"/manage/{public_id}"
        assert published == [(manage_channel(public_id), "turnout", "console")]
        assert self.flags(world, poll_id) == (True, True)
        page = unescape(admin.get(f"/manage/{public_id}").text)
        assert "Résultat affiché à l'écran." in page
        assert re.search(
            r'<span class="badge success" data-on-screen>.*?À l\'écran</span>', page, re.S
        )
        assert 'name="shown" value="0"' in page and "Retirer de l'écran" in page
        view = admin.get(screen).text
        assert "Adoptée" in view and "Détail des voix non affiché" in view

        # Hidden from voters: the screen keeps the result.
        post(admin, f"{base}/dismiss")
        assert self.flags(world, poll_id) == (False, True)
        assert "Adoptée" in admin.get(screen).text

        post(admin, f"{base}/screen", {"shown": "0"})
        assert self.flags(world, poll_id) == (False, False)
        page = unescape(admin.get(f"/manage/{public_id}").text)
        assert "Résultat retiré de l'écran." in page and "data-on-screen" not in page
        assert "Publier le résultat aux votants" in page and "Masquer le résultat" not in page
        assert "Adoptée" not in admin.get(screen).text
        # The session log records both, by their labels (FR-80).
        assert "Résultat affiché à l'écran</td>" in page
        assert "Résultat retiré de l'écran</td>" in page

    def test_no_detail_note_from_ten_ballots(
        self, admin: TestClient, world: World, owner: User, public_id: str
    ) -> None:
        self.closed_poll(world, owner, public_id, 10)
        page = unescape(admin.get(f"/manage/{public_id}").text)
        assert "Afficher sur l'écran" in page
        assert "l'écran n'affiche que le verdict" not in page

    def test_open_and_cancelled_polls_are_refused(
        self, admin: TestClient, world: World, owner: User, public_id: str
    ) -> None:
        cancelled = self.closed_poll(world, owner, public_id, 1)
        poll_action(world, owner, "cancel_poll", cancelled, reason="Erreur")
        opened = world.poll(owner, public_id)
        page = unescape(admin.get(f"/manage/{public_id}").text)
        assert f'action="/manage/{public_id}/polls/{cancelled}/screen"' not in page
        assert f'action="/manage/{public_id}/polls/{opened}/screen"' not in page
        for poll_id, message in (
            (opened, "Ce vote est ouvert."),
            (cancelled, "Ce vote a été annulé."),
        ):
            response = post(admin, f"/manage/{public_id}/polls/{poll_id}/screen", {"shown": "1"})
            assert response.status_code == 303
            assert message in unescape(admin.get(f"/manage/{public_id}").text)
            assert self.flags(world, poll_id) == (False, False)

    def test_csrf_isolation_and_delegates(
        self, app: FastAPI, client: TestClient, world: World, owner: User, public_id: str
    ) -> None:
        from tests.web.manage.test_delegates import make_link, open_link

        poll_id = self.closed_poll(world, owner, public_id, 1)
        url = f"/manage/{public_id}/polls/{poll_id}/screen"
        make_account(world, "intruder@example.org")
        log_in(client, "intruder@example.org")
        assert post(client, url, {"shown": "1"}).status_code == 404
        log_in(client, owner.email)
        assert client.post(url, data={"shown": "1"}).status_code == 403  # no CSRF token
        assert self.flags(world, poll_id) == (False, False)
        # A delegate without the polls right: 404; with it: shown, logged as the delegate.
        _, timer_token = make_link(world, owner, public_id, ["timer"], name="Paul")
        assert post(open_link(app, public_id, timer_token), url, {"shown": "1"}).status_code == 404
        delegate_id, token = make_link(world, owner, public_id, ["polls"])
        browser = open_link(app, public_id, token)
        page = unescape(browser.get(f"/manage/{public_id}").text)
        assert "Afficher sur l'écran" in page
        assert post(browser, url, {"shown": "1"}).status_code == 303
        assert self.flags(world, poll_id) == (False, True)
        with world.database.read() as db:
            entry = db.scalars(
                select(AuditEntry).where(AuditEntry.action == "poll.screen_shown")
            ).one()
            assert entry.actor == "delegate"
            assert entry.details == {"poll_id": poll_id, "delegate": delegate_id}
