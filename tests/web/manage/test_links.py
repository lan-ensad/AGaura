"""Console controls of the secret links: projection screen (FR-16), public results (FR-17)."""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from agaura.models import AccessMode, AuditEntry, User
from agaura.services import access, assemblies, polls, timer
from tests.conftest import RESOLUTION, World
from tests.web.conftest import log_in, make_account, post
from tests.web.manage.test_delegates import card_of, make_link, open_link
from tests.web.manage.test_layout import text_of
from tests.web.screen.conftest import screen_url as screen_address
from tests.web.screen.test_results import CHOOSE, ELECT, closed


def audit_actions(world: World) -> list[str]:
    with world.database.read() as db:
        return list(db.scalars(select(AuditEntry.action).order_by(AuditEntry.id)))


def tokens(world: World, owner: User, public_id: str) -> tuple[str, str | None]:
    with world.database.read() as db:
        return (
            access.screen_token(db, world.keyring, owner, public_id),
            access.public_results_token(db, world.keyring, owner, public_id),
        )


@pytest.fixture
def public_id(world: World, owner: User) -> str:
    return world.assembly(owner)


@pytest.fixture
def published(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """Events published on the real-time hub."""
    events: list[tuple[str, str, str]] = []
    hub = app.state.hub
    original = hub.publish

    def record(channel: str, event: str, data: str = "") -> None:
        events.append((channel, event, data))
        original(channel, event, data)

    monkeypatch.setattr(hub, "publish", record)
    return events


def test_console_shows_the_screen_link(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    screen, results = tokens(world, owner, public_id)
    page = admin.get(f"/manage/{public_id}").text
    url = f"http://testserver/{public_id}/screen/{screen}"
    assert f'value="{url}"' in page
    assert re.search(
        rf'<a [^>]*href="{re.escape(url)}" target="_blank" rel="noopener noreferrer">', page
    )
    assert "Écran de projection" in page and "Nouveau lien d'écran" in page
    # What the screen shows: a result only when the tribune chooses to (FR-72, FR-75), told
    # in words next to the link.
    assert "n'affiche un résultat que si la tribune le choisit, vote par vote" in page
    assert "Ce que la salle voit" in page
    assert "n'affiche jamais de résultat" not in page
    assert "résultats publiés à l'écran" not in page
    assert "data-confirm=\"Créer un nouveau lien d'écran\u00a0?" in page
    assert results is None
    assert "/results/" not in page and "Activer le lien public" in page


def test_regenerating_the_screen_link(
    admin: TestClient,
    world: World,
    owner: User,
    public_id: str,
    published: list[tuple[str, str, str]],
) -> None:
    old, _ = tokens(world, owner, public_id)
    assert admin.get(f"/{public_id}/screen/{old}").status_code == 200
    response = post(admin, f"/manage/{public_id}/screen-link")
    assert response.status_code == 303
    assert response.headers["location"] == f"/manage/{public_id}#links"
    new, _ = tokens(world, owner, public_id)
    assert new != old
    # A screen open in the same browser does not swallow the console's notice.
    assert admin.get(f"/{public_id}/screen/{new}/state").status_code == 200
    page = admin.get(f"/manage/{public_id}").text
    assert "Nouveau lien d&#39;écran créé\u00a0: l&#39;ancien ne fonctionne plus." in page
    assert f"/screen/{new}" in page and f"/screen/{old}" not in page
    assert "Lien de l&#39;écran régénéré" in page  # session log
    assert audit_actions(world)[-1] == "access.screen_link_regenerated"
    # The open screen refreshes on this event, gets a 404 and reloads into the 404 page.
    assert (f"manage:{public_id}", "turnout", "console") in published
    assert admin.get(f"/{public_id}/screen/{old}/state").status_code == 404
    assert admin.get(f"/{public_id}/screen/{old}").status_code == 404


def test_public_results_link(admin: TestClient, world: World, owner: User, public_id: str) -> None:
    response = post(admin, f"/manage/{public_id}/public-results", {"enabled": "1"})
    assert response.status_code == 303
    assert response.headers["location"] == f"/manage/{public_id}#public-results"
    _, token = tokens(world, owner, public_id)
    assert token is not None
    page = admin.get(f"/manage/{public_id}").text
    assert "Lien public des résultats activé." in page
    assert f'value="http://testserver/{public_id}/results/{token}"' in page
    assert "Désactiver le lien" in page

    # Enabling again (another tab, a double click) keeps the link already shared.
    post(admin, f"/manage/{public_id}/public-results", {"enabled": "1"})
    assert tokens(world, owner, public_id)[1] == token

    post(admin, f"/manage/{public_id}/public-results", {"enabled": "0"})
    assert tokens(world, owner, public_id)[1] is None
    page = admin.get(f"/manage/{public_id}").text
    assert "Lien public des résultats désactivé\u00a0: il ne fonctionne plus." in page
    assert "/results/" not in page
    actions = audit_actions(world)
    assert actions.count("access.public_link_enabled") == 1
    assert actions[-1] == "access.public_link_disabled"

    # A new link after disabling: the old one never comes back.
    post(admin, f"/manage/{public_id}/public-results", {"enabled": "1"})
    assert tokens(world, owner, public_id)[1] not in (None, token)


def test_links_work_in_every_state(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner, in_session=False)
    assert post(admin, f"/manage/{public_id}/screen-link").status_code == 303
    assert post(admin, f"/manage/{public_id}/close").status_code == 303  # refused: preparation
    assert admin.get(f"/manage/{public_id}").status_code == 200


def test_tokens_never_reach_the_log(
    admin: TestClient, world: World, owner: User, public_id: str
) -> None:
    post(admin, f"/manage/{public_id}/screen-link")
    post(admin, f"/manage/{public_id}/public-results", {"enabled": "1"})
    screen, results = tokens(world, owner, public_id)
    with world.database.read() as db:
        entries = db.scalars(select(AuditEntry)).all()
        dumped = " ".join(f"{entry.action} {entry.details}" for entry in entries)
    assert screen not in dumped and results is not None and results not in dumped


def test_csrf_and_isolation(client: TestClient, world: World, owner: User, public_id: str) -> None:
    make_account(world, "intruder@example.org")
    log_in(client, "intruder@example.org")
    before = tokens(world, owner, public_id)
    assert post(client, f"/manage/{public_id}/screen-link").status_code == 404
    assert post(client, f"/manage/{public_id}/public-results", {"enabled": "1"}).status_code == 404
    log_in(client, owner.email)
    assert client.post(f"/manage/{public_id}/screen-link").status_code == 403  # no CSRF token
    assert tokens(world, owner, public_id) == before


def console(client: TestClient, public_id: str) -> str:
    return client.get(f"/manage/{public_id}").text


def toggles(card: str) -> list[tuple[str, str, str | None, str]]:
    """The buttons of "Ce que la salle voit": (part, shown asked for, poll, accessible name)."""
    found = []
    for form in re.findall(r'<form[^>]*class="inline recap-toggle".*?</form>', card, re.S):
        fields = dict(re.findall(r'name="(part|shown|poll)" value="([^"]*)"', form))
        name = re.search(r'aria-label="([^"]*)"', form)
        assert name is not None and fields["part"]
        label = name[1].replace("&#34;", '"').replace("&#39;", "'")
        found.append((fields["part"], fields["shown"], fields.get("poll"), label))
    return found


def recap(admin: TestClient, public_id: str) -> str:
    """The text of "Ce que la salle voit", in the Projection screen rubric."""
    return text_of(card_of(admin.get(f"/manage/{public_id}").text, "screen-now"))


def test_what_the_room_sees_names_only_the_results_shown(
    admin: TestClient, world: World, owner: User
) -> None:
    """The recap names a result only when the tribune shows it on the screen, for a closed
    poll (FR-72, FR-75): never a poll open now, a result published to voters only, or a
    cancelled poll; with the count of votes from 10 ballots, the verdict alone below."""
    public_id = world.assembly(owner)
    shown = closed(world, owner, public_id, RESOLUTION, [{"choice": 1}] * 10)
    small = closed(world, owner, public_id, ELECT, [{"approved": [1, 2]}] * 3)
    voters_only = closed(world, owner, public_id, CHOOSE, [{"choice": 2}] * 10, show=False)
    cancelled = closed(world, owner, public_id, CHOOSE, [{"choice": 1}] * 10)
    world.poll(owner, public_id)  # open now: its title and turnout, never a result
    with world.database.write() as db:
        polls.publish_result(db, owner, voters_only, voters=True)
        polls.cancel_poll(db, owner, cancelled, "Erreur")
        polls.show_on_screen(db, owner, shown, shown=False)
        polls.show_on_screen(db, owner, shown, shown=True)  # the latest shown: on the stage
    text = recap(admin, public_id)
    assert (
        "1 vote en cours\u00a0: titre, options et participation (0 bulletin / 33 présents)" in text
    )
    assert (
        "Résultat affiché\u00a0: Approbation des comptes, Adoptée, avec le détail des voix "
        "(10 bulletins) Masquer"
    ) in text
    assert (
        "Résultat sur une ligne\u00a0: Élection du conseil, Élus\u00a0: Alice, Bruno Masquer"
        in text
    )
    # Published to voters only: a result the tribune may show; cancelled: never named.
    assert "Résultat non affiché\u00a0: Lieu de la prochaine AG Afficher" in text
    assert text.count("Lieu de la prochaine AG") == 1
    assert "Aucun résultat affiché" not in text
    card = card_of(admin.get(f"/manage/{public_id}").text, "screen-now")
    assert toggles(card) == [
        (
            "result",
            "0",
            str(shown),
            "Masquer\u00a0: résultat de «\u00a0Approbation des comptes\u00a0»",
        ),
        ("result", "0", str(small), "Masquer\u00a0: résultat de «\u00a0Élection du conseil\u00a0»"),
        ("join", "0", None, "Masquer\u00a0: comment rejoindre"),
        ("timer", "1", None, "Afficher\u00a0: chronomètre"),
        (
            "result",
            "1",
            str(voters_only),
            "Afficher\u00a0: résultat de «\u00a0Lieu de la prochaine AG\u00a0»",
        ),
    ]
    assert f'value="{cancelled}"' not in card

    with world.database.write() as db:
        polls.show_on_screen(db, owner, shown, shown=False)
        polls.show_on_screen(db, owner, small, shown=False)
    text = recap(admin, public_id)
    assert "Résultat affiché" not in text and "Résultat sur une ligne" not in text
    assert "Aucun résultat affiché" in text
    assert "Résultat non affiché\u00a0: Approbation des comptes Afficher" in text
    assert "Jamais de résultat d'un vote ouvert ou annulé" in text


def test_what_the_room_sees_follows_the_screen(
    admin: TestClient, world: World, owner: User
) -> None:
    """Entries, the way to join, the chronometer, the agenda and the closing message, in the
    words of the screen, each block with its button."""
    public_id = world.assembly(owner, in_session=False, quorum_rule="2")
    text = recap(admin, public_id)
    assert "En-tête\u00a0: titre, «\u00a0Ouverture prochaine\u00a0»" in text
    assert "Pour rejoindre\u00a0: l'adresse, le QR code et le mot de passe de la session" in text
    assert "Chronomètre\u00a0: non affiché Afficher" in text
    assert "Message" not in text  # joining takes the stage
    assert [part for part, *_ in toggles(card_of(console(admin, public_id), "screen-now"))] == [
        "join",
        "timer",
    ]

    with world.database.write() as db:
        assemblies.open_assembly(db, owner, public_id)
        assembly = assemblies.get_assembly(db, owner, public_id)
        assembly.agenda = [{"title": "Accueil"}, {"title": "Rapport moral"}]
        assembly.agenda_current = 2
        timer.start(db, owner, public_id, duration=180, label="Temps de parole", show=True)
        access.set_entries_locked(db, owner, public_id, True)
    text = recap(admin, public_id)
    assert "En-tête\u00a0: titre, présents et quorum, «\u00a0Entrées fermées\u00a0»" in text
    assert "Chronomètre\u00a0: Temps de parole, en marche" in text
    assert "Ordre du jour, point 2\u00a0: Rapport moral" in text
    assert "Pour rejoindre\u00a0: masqué tant que les entrées sont verrouillées" in text
    # Locked entries hide the way to join: the lock, not a button of this card, shows it again.
    assert [part for part, *_ in toggles(card_of(console(admin, public_id), "screen-now"))] == [
        "timer",
        "agenda",
    ]

    with world.database.write() as db:
        assemblies.close_assembly(db, owner, public_id)
    text = recap(admin, public_id)
    assert (
        "Message\u00a0: «\u00a0La session est terminée. Merci de votre participation.\u00a0»"
        in text
    )
    assert "Pour rejoindre\u00a0: masqué, la session est terminée" in text
    assert "Chronomètre" not in text and "Ordre du jour" not in text
    assert toggles(card_of(console(admin, public_id), "screen-now")) == []

    codes = world.assembly(owner, mode=AccessMode.CODES)
    assert "l'adresse et la consigne des cartes de codes" in recap(admin, codes)


def test_each_block_is_hidden_and_shown_from_the_card(
    admin: TestClient, world: World, owner: User
) -> None:
    """The buttons of "Ce que la salle voit" act on the screen itself (FR-70): the agenda, the
    way to join, the chronometer and a result; the screen gives the address at its foot once
    the way to join is hidden, and never receives the password then."""
    public_id = world.assembly(owner)
    poll_id = closed(world, owner, public_id, RESOLUTION, [{"choice": 1}] * 3, show=False)
    with world.database.write() as db:
        assembly = assemblies.get_assembly(db, owner, public_id)
        assembly.agenda = [{"title": "Accueil"}, {"title": "Rapport moral"}]
    screen_url = screen_address(world, owner, public_id)

    def hide_or_show(part: str, shown: str, **extra: str) -> None:
        data = {"part": part, "shown": shown, **extra}
        response = post(admin, f"/manage/{public_id}/screen/show", data)
        assert response.status_code == 303
        assert response.headers["location"] == f"/manage/{public_id}#screen-now"

    page = admin.get(screen_url).text
    assert "data-password" in page and "Rapport moral" in page and "screen-foot" not in page
    hide_or_show("join", "0")
    hide_or_show("agenda", "0")
    text = recap(admin, public_id)
    assert (
        "Pour rejoindre\u00a0: masqué par la tribune\u00a0; l'adresse reste en bas de l'écran"
        in text
    )
    assert "Ordre du jour\u00a0: masqué Afficher" in text
    page = admin.get(screen_url).text
    assert "data-password" not in page and "Rapport moral" not in page
    assert 'class="screen-foot"' in page and "Entrées ouvertes" in page
    hide_or_show("timer", "1")
    hide_or_show("result", "1", poll=str(poll_id))
    page = admin.get(screen_url).text
    assert "data-timer-clock" in page and f'data-result="{poll_id}"' in page
    text = recap(admin, public_id)
    assert "Chronomètre\u00a0: Chronomètre, prêt à démarrer Masquer" in text
    hide_or_show("join", "1")
    hide_or_show("agenda", "1")
    hide_or_show("agenda", "1")  # asked twice: logged once
    hide_or_show("timer", "0")
    hide_or_show("result", "0", poll=str(poll_id))
    page = admin.get(screen_url).text
    assert "data-password" in page and "Rapport moral" in page and "data-timer-clock" not in page
    assert f'data-result="{poll_id}"' not in page
    actions = [action for action in audit_actions(world) if action.startswith("screen.")]
    assert actions == [
        "screen.join_hidden",
        "screen.agenda_hidden",
        "screen.join_shown",
        "screen.agenda_shown",
    ]
    log = admin.get(f"/manage/{public_id}").text
    assert "Accès masqué à l&#39;écran" in log and "Ordre du jour affiché à l&#39;écran" in log


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"part": "questions", "shown": "0"}, "Cette partie de l&#39;écran ne peut être"),
        ({"part": "", "shown": "0"}, "Cette partie de l&#39;écran ne peut être"),
        ({"part": "result", "shown": "1"}, "Cette partie de l&#39;écran ne peut être"),
        ({"part": "result", "shown": "1", "poll": "x"}, "Saisissez un nombre entier."),
    ],
)
def test_refused_blocks(
    admin: TestClient, world: World, owner: User, data: dict[str, str], message: str
) -> None:
    public_id = world.assembly(owner)
    response = post(admin, f"/manage/{public_id}/screen/show", data)
    assert response.status_code == 303
    assert message in admin.get(response.headers["location"]).text


def test_a_result_of_another_session_or_an_open_poll(
    admin: TestClient, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    other = world.assembly(owner)
    elsewhere = closed(world, owner, other, RESOLUTION, [{"choice": 1}], show=False)
    data = {"part": "result", "shown": "1", "poll": str(elsewhere)}
    assert post(admin, f"/manage/{public_id}/screen/show", data).status_code == 404
    open_now = world.poll(owner, public_id)
    data["poll"] = str(open_now)
    response = post(admin, f"/manage/{public_id}/screen/show", data)
    assert response.status_code == 303
    with world.database.read() as db:
        assert not polls.get_poll(db, owner, open_now).shown_on_screen


def test_hidden_blocks_until_the_session_ends(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    with world.database.write() as db:
        assemblies.close_assembly(db, owner, public_id)
    response = post(admin, f"/manage/{public_id}/screen/show", {"part": "agenda", "shown": "0"})
    assert response.status_code == 303
    assert "La session est terminée." in admin.get(response.headers["location"]).text


def test_a_delegate_link_with_the_right_gets_the_card_without_the_links(
    app: FastAPI, admin: TestClient, world: World, owner: User
) -> None:
    """The right "Ce que la salle voit" (FR-92): the card and its buttons, alone in the rubric
    of the screen; never the screen link nor the public results link."""
    public_id = world.assembly(owner)
    poll_id = closed(world, owner, public_id, RESOLUTION, [{"choice": 1}] * 3, show=False)
    _, token = make_link(world, owner, public_id, ["screen"], name="Ali, régie")
    browser = open_link(app, public_id, token)
    page = console(browser, public_id)
    card = card_of(page, "screen-now")
    assert 'id="tab-screen"' in page and 'href="#tab-screen"' in page
    assert 'id="links"' not in page and "/screen/" not in page.replace("/screen/show", "")
    assert 'id="timer"' not in page and "data-hand-over" not in card
    assert (
        "result",
        "1",
        str(poll_id),
        "Afficher\u00a0: résultat de «\u00a0Approbation des comptes\u00a0»",
    ) in (toggles(card))
    data = {"part": "result", "shown": "1", "poll": str(poll_id)}
    assert post(browser, f"/manage/{public_id}/screen/show", data).status_code == 303
    with world.database.read() as db:
        assert polls.get_poll(db, owner, poll_id).shown_on_screen
    # Other rights do not open the card, nor its route.
    _, other = make_link(world, owner, public_id, ["polls", "timer"], name="Claire")
    browser = open_link(app, public_id, other)
    assert 'id="screen-now"' not in console(browser, public_id)
    data["shown"] = "0"
    assert post(browser, f"/manage/{public_id}/screen/show", data).status_code == 404


def test_the_card_hands_over_what_the_room_sees(
    admin: TestClient, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    card = card_of(console(admin, public_id), "screen-now")
    assert 'data-hand-over="screen-now"' in card
    assert 'name="rights" value="screen" checked' in card
    assert 'aria-label="Confier ce que la salle voit à une personne"' in card


def test_the_list_follows_the_screen_in_the_live_panel(
    app: FastAPI, admin: TestClient, world: World, owner: User
) -> None:
    """The list of "Ce que la salle voit" is a live part (GET …/panel): for the owner and a
    delegate holding the right "screen", never for another delegate."""
    public_id = world.assembly(owner)
    panel = admin.get(f"/manage/{public_id}/panel").text
    assert (
        '<div data-live-part="screen-now">' in panel and "Chronomètre\u00a0: non affiché" in panel
    )
    with world.database.write() as db:
        timer.show(db, owner, public_id, True)
    panel = admin.get(f"/manage/{public_id}/panel").text
    assert "Masquer\u00a0: chronomètre" in panel and 'id="screen-toggle-timer"' in panel
    _, token = make_link(world, owner, public_id, ["screen"], name="Ali")
    assert (
        'data-live-part="screen-now"'
        in open_link(app, public_id, token).get(f"/manage/{public_id}/panel").text
    )
    _, other = make_link(world, owner, public_id, ["timer"], name="Claire")
    browser = open_link(app, public_id, other)
    assert 'data-live-part="screen-now"' not in browser.get(f"/manage/{public_id}/panel").text


def test_a_button_sent_by_the_script_acts_in_place(
    admin: TestClient, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    card = card_of(console(admin, public_id), "screen-now")
    assert card.count("data-live-form") == len(toggles(card))
    headers = {"X-Requested-With": "fetch"}
    data = {"part": "agenda", "shown": "0"}
    response = post(admin, f"/manage/{public_id}/screen/show", data, headers=headers)
    assert response.status_code == 204
    with world.database.read() as db:
        assert assemblies.get_assembly(db, owner, public_id).screen_agenda_hidden
    refused = post(admin, f"/manage/{public_id}/screen/show", {"part": "x"}, headers=headers)
    assert refused.status_code == 400
