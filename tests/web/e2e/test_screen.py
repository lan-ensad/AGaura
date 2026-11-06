"""Projection screen in a browser (FR-70 to FR-75): a 16:9 screen follows the console, the
phones and a chronometer run from a phone, shows no result until the tribune shows one and
takes it off, then stops working once its link is regenerated."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, expect

from agaura.models import User
from agaura.services import assemblies, polls, timer
from agaura.services.polls import PollInput
from tests.conftest import RESOLUTION, World
from tests.web.e2e.conftest import ErrorWatch, new_phone, show_rubric, unfold
from tests.web.e2e.test_journeys import console, launch_poll

pytestmark = pytest.mark.e2e

# Directory for screenshots to check readability by eye, e.g.
# ``AGAURA_SCREENSHOTS=/tmp/shots uv run pytest tests/web/e2e/test_screen.py``.
SHOTS = os.environ.get("AGAURA_SCREENSHOTS")

AGENDA = [
    "Accueil et émargement",
    "Rapport moral de la présidente",
    "Rapport financier 2025",
    "Approbation des comptes et affectation du résultat",
    "Budget prévisionnel 2026",
    "Montant de la cotisation",
    "Élection au conseil d'administration",
    "Questions diverses",
]


TIE = PollInput("Lieu de la prochaine AG", "single_choice", ["Salle", "Parc", "Ferme"])
ADOPT = PollInput(
    "Cotisation 2027",
    "single_choice",
    ["Pour", "Contre"],
    params={"adopt_option": 1},
    blank_label="Abstention",
)


def vote_and_close(
    world: World, owner: User, public_id: str, data: PollInput, choices: list[int | None]
) -> int:
    """A poll voted by new voters (one choice each, None: blank), then closed."""
    poll_id = world.poll(owner, public_id, data)
    for token, choice in zip(world.voters(owner, public_id, len(choices)), choices, strict=True):
        world.vote(public_id, token, poll_id, None if choice is None else {"choice": choice})
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
    return poll_id


def check_layout(page: Page, name: str) -> None:
    """At both projector resolutions: nothing scrolls; screenshots when asked."""
    for width, height in ((1920, 1080), (1280, 720)):
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(300)  # resize handler: refit the boxes
        assert page.evaluate("document.documentElement.scrollHeight") <= height
        assert page.evaluate("document.documentElement.scrollWidth") <= width
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(SHOTS) / f"e2e-{name}-{width}.png"))
    page.set_viewport_size({"width": 1920, "height": 1080})


def clock_seconds(page: Page) -> int:
    """The chronometer of the screen, in seconds ("+m:ss" counts as negative)."""
    text = page.locator(".screen-timer [data-timer-clock]").inner_text().strip()
    match = re.fullmatch(r"(\+?)(\d+):(\d\d)", text)
    assert match is not None, text
    seconds = int(match.group(2)) * 60 + int(match.group(3))
    return -seconds if match.group(1) else seconds


@pytest.fixture
def projector(browser: Browser, live_server: str) -> Iterator[BrowserContext]:
    context = browser.new_context(base_url=live_server, viewport={"width": 1920, "height": 1080})
    yield context
    context.close()


@pytest.fixture
def tribune_phone(browser: Browser, pw: Playwright, live_server: str) -> Iterator[BrowserContext]:
    """A member of the tribune running the chronometer from a phone."""
    context = new_phone(browser, pw, live_server)
    yield context
    context.close()


def test_screen_follows_the_session(
    desktop: BrowserContext,
    projector: BrowserContext,
    phone: BrowserContext,
    tribune_phone: BrowserContext,
    errors: ErrorWatch,
    world: World,
    owner: User,
) -> None:
    public_id = world.assembly(owner, title="AG de l'association", quorum_rule="1")
    with world.database.write() as db:  # the agenda console belongs to another area
        assembly = assemblies.get_assembly(db, owner, public_id)
        assembly.agenda = [{"title": title} for title in AGENDA]
        assembly.agenda_current = 2
    password = world.password(owner, public_id)
    admin = console(desktop, errors, owner, public_id)
    screen_url = admin.locator("#links-screen-url").input_value()
    assert f"/{public_id}/screen/" in screen_url

    screen = errors.watch(projector.new_page())
    screen.goto(screen_url)
    expect(screen.get_by_role("heading", name="AG de l'association")).to_be_visible()
    expect(screen.locator("[data-entries]")).to_have_text("Entrées ouvertes")
    expect(screen.locator("[data-password]")).to_have_text(password)
    expect(screen.locator(".join-qr svg")).to_be_visible()
    expect(screen.locator("[data-attendees]")).to_have_text("0")
    expect(screen.locator(".quorum")).to_contain_text("non atteint")
    current = screen.locator(".agenda-list li.current")
    expect(current).to_contain_text("Rapport moral de la présidente")
    expect(current).to_contain_text("En cours")
    check_layout(screen, "join-agenda")

    # A phone joins: the attendee count follows.
    voter = errors.watch(phone.new_page())
    voter.goto(f"/{public_id}#k={password}")
    expect(voter.get_by_text("En attente du prochain vote")).to_be_visible()
    expect(screen.locator("[data-attendees]")).to_have_text("1")
    expect(screen.locator(".quorum")).to_contain_text("atteint")

    # A pushed poll appears on the screen within 2 s (NFR-03), with its turnout and countdown.
    started = launch_poll(
        admin, "Résolution", "Approbation des comptes", fields={"#editor-minutes": "5"}
    )
    expect(screen.get_by_role("heading", name="Approbation des comptes")).to_be_visible()
    assert time.monotonic() - started < 2.0
    expect(screen.locator("[data-countdown-live]")).to_contain_text("Clôture dans 0")
    expect(screen.locator("[data-countdown-fixed]")).to_be_hidden()
    expect(screen.locator("[data-turnout]")).to_have_text("0 bulletin / 1 présent")
    expect(screen.locator("[data-password]")).to_be_visible()  # entries still open

    # The phone votes: the turnout follows; no partial result is shown (D4).
    voter.get_by_text("Pour", exact=True).click()
    voter.get_by_role("button", name="Continuer").click()
    voter.get_by_role("button", name="Confirmer mon vote").click()
    expect(voter.get_by_text("Vote enregistré")).to_be_visible()
    expect(screen.locator("[data-turnout]")).to_have_text("1 bulletin / 1 présent")

    # A one-minute speaking time set up and started from a phone of the tribune: one start
    # button; the mode decides what is asked; tapping a duration starts nothing.
    tribune = console(tribune_phone, errors, owner, public_id)
    show_rubric(tribune, "meeting")  # the card and its settings, above the tribune (D-A)
    card = tribune.locator("#timer")
    expect(card.get_by_role("button", name="Démarrer")).to_have_count(1)
    # The settings fold under the line of the last choice: a countdown of 3 minutes first.
    summary = card.locator("[data-timer-summary]")
    expect(summary).to_have_text(re.compile(r"^\s*Compte à rebours\s+3 min\s*$"))
    durations = card.locator("[data-countdown-fields]")
    expect(durations).to_be_hidden()
    card.get_by_text("Modifier les réglages").click()
    expect(durations).to_be_visible()
    card.locator("label.segment", has_text="Chronomètre").click()
    expect(durations).to_be_hidden()
    expect(summary.locator("[data-summary-mode]")).to_have_text("Chronomètre")
    expect(summary.locator("[data-summary-duration]")).to_be_hidden()
    card.locator("label.segment", has_text="Compte à rebours").click()
    expect(durations).to_be_visible()
    card.locator("label.chip", has_text="1 min").click()
    expect(card.locator("input[name=duration][value='60']")).to_be_checked()
    card.locator("#timer-label").fill("Temps de parole")
    expect(summary).to_have_text(re.compile(r"Compte à rebours\s+1 min\s+Temps de parole"))
    tribune.wait_for_timeout(500)
    expect(screen.locator(".screen-timer")).to_have_count(0)  # nothing started yet
    started = time.monotonic()
    card.get_by_role("button", name="Démarrer").click()
    timer = screen.locator(".screen-timer")
    expect(timer).to_contain_text("Temps de parole")
    assert time.monotonic() - started < 2.0
    expect(tribune.get_by_text("Compte à rebours démarré.")).to_be_visible()
    expect(tribune.locator("#timer").get_by_role("button", name="Démarrer")).to_have_count(0)
    first = clock_seconds(screen)
    assert 55 <= first <= 60
    expect(tribune.locator("#timer [data-timer-clock]")).not_to_have_text(
        re.compile(r"^1:00$"), timeout=3000
    )
    expect(admin.locator("#timer")).to_contain_text("En marche", timeout=5000)
    screen.wait_for_timeout(1200)
    assert clock_seconds(screen) < first
    expect(screen.get_by_text("Adoptée")).to_have_count(0)
    check_layout(screen, "poll-timer-agenda")

    # Pause from the phone: the screen shows it, and the time stops.
    tribune.locator("#timer").get_by_role("button", name="Pause").click()
    expect(tribune.get_by_text("En pause\u00a0: le temps est arrêté.")).to_be_visible()
    expect(timer).to_contain_text("En pause")
    paused = clock_seconds(screen)
    screen.wait_for_timeout(1200)
    assert clock_seconds(screen) == paused

    # Close and publish from Steering's "Vote clos" command (D-K): voters see the result; the
    # screen only says the vote is closed.
    admin.locator("#live-panel").get_by_role("button", name="Clore le vote").click()
    expect(admin.get_by_text("Vote clos\u00a0; résultat calculé.")).to_be_visible()
    expect(admin.locator("#tab-cockpit")).to_be_visible()  # back where it was closed
    expect(screen.get_by_role("heading", name="Approbation des comptes")).to_be_hidden()
    finished = admin.locator(".finished-poll", has_text="Approbation des comptes")
    expect(finished.locator("input[name=screen]")).to_have_count(0)
    closed = admin.locator("#just-closed")
    expect(closed).to_contain_text("Approbation des comptes")
    expect(closed.locator(".verdict")).to_have_text("Adoptée")
    closed.get_by_role("button", name="Publier le résultat aux votants").click()
    expect(voter.get_by_text("Adoptée")).to_be_visible()
    expect(admin.locator("#tab-cockpit")).to_be_visible()
    expect(admin.locator("#just-closed")).to_have_count(0)  # published: the command goes
    show_rubric(admin, "polls")
    past = screen.locator(".past-list li", has_text="Approbation des comptes")
    expect(past).to_contain_text("Vote clos")
    expect(screen.get_by_text("Adoptée")).to_have_count(0)
    assert "Adoptée" not in screen.content() and " %" not in screen.content()

    # Until the tribune shows it (FR-75): one button, and within a second the night card, with
    # the verdict alone under 10 ballots; "Retirer de l'écran" takes it off.
    # Timed from the console action sent (the click returns once the console's form is
    # answered): the screen's own delay, as FR-73 measures it.
    unfold(finished)  # a tile, folded by default; the action's address unfolds it again after
    finished.get_by_role("button", name="Afficher sur l'écran").click()
    started = time.monotonic()
    result = screen.locator(".screen-result")
    expect(result.locator(".screen-verdict")).to_have_text("Adoptée")
    assert time.monotonic() - started < 1.0
    expect(admin.get_by_text("Résultat affiché à l'écran.")).to_be_visible()
    expect(finished.locator("[data-on-screen]")).to_have_text("À l'écran")
    expect(result).to_contain_text("Détail des voix non affiché\u00a0: moins de 10 bulletins")
    expect(result.locator(".screen-tally")).to_have_count(0)
    assert " %" not in screen.content()
    check_layout(screen, "result-verdict-only")
    finished.get_by_role("button", name="Retirer de l'écran").click()
    started = time.monotonic()
    expect(result).to_have_count(0)
    assert time.monotonic() - started < 1.0
    expect(past).to_contain_text("Vote clos")
    assert "Adoptée" not in screen.content()

    # A tie with the count of each option, then a resolution shown after it: the latest takes
    # the main place, the tie keeps one line; neither scrolls the screen.
    tie = vote_and_close(world, owner, public_id, TIE, [2] * 6 + [3] * 6 + [1] * 3)
    adopted = vote_and_close(world, owner, public_id, ADOPT, [1] * 9 + [2] * 2 + [None])
    admin.reload()
    show_rubric(admin, "polls")
    unfold(admin.locator(f"#poll-{tie}"))
    admin.locator(f"#poll-{tie}").get_by_role("button", name="Afficher sur l'écran").click()
    expect(result.locator(".screen-verdict")).to_have_text(
        "Égalité\u00a0: à trancher selon les statuts"
    )
    expect(result).to_contain_text("À égalité\u00a0: Parc, Ferme.")
    expect(result.locator(".tag-tie")).to_have_count(2)
    check_layout(screen, "result-tie")
    unfold(admin.locator(f"#poll-{adopted}"))
    admin.locator(f"#poll-{adopted}").get_by_role("button", name="Afficher sur l'écran").click()
    expect(result.locator("h2")).to_have_text("Cotisation 2027")
    blank = result.locator(".screen-counts div", has_text="Abstention").locator("dd")
    expect(blank).to_have_text("1")
    expect(screen.locator(".past-list li", has_text="Lieu de la prochaine AG")).to_contain_text(
        "Égalité\u00a0: à trancher selon les statuts"
    )
    check_layout(screen, "result-detail-and-line")
    for poll_id in (tie, adopted):
        unfold(admin.locator(f"#poll-{poll_id}"))
        admin.locator(f"#poll-{poll_id}").get_by_role("button", name="Retirer de l'écran").click()
        expect(admin.locator(f"#poll-{poll_id} [data-on-screen]")).to_have_count(0)
    expect(result).to_have_count(0)
    expect(screen.locator(".past-verdict")).to_have_count(0)

    # Reset from the command of Steering, then a three-second countdown set up in the card
    # ("Autre durée", chosen by typing in its fields): past zero, "+0:0x" and a text.
    show_rubric(admin, "cockpit")
    admin.locator("#cmd-timer").get_by_role("button", name="Réinitialiser").click()
    expect(admin.get_by_text("Réinitialisé\u00a0: préparez le suivant.")).to_be_visible()
    expect(admin.locator("#tab-cockpit")).to_be_visible()
    expect(admin.locator("#cmd-timer")).to_contain_text("1 min")  # the last choice, to restart
    show_rubric(admin, "meeting")
    setup = admin.locator("#timer form.timer-setup")
    expect(setup.locator("input[name=duration][value='60']")).to_be_checked()  # remembered
    expect(setup.locator("[data-timer-summary]")).to_contain_text("1 min")
    setup.get_by_text("Modifier les réglages").click()
    expect(setup.locator("input[name=minutes]")).to_be_hidden()  # only with "Autre durée"
    setup.locator("label.chip", has_text="Autre durée").click()
    expect(setup.locator("input[name=minutes]")).to_be_focused()
    setup.locator("input[name=minutes]").fill("0")
    setup.locator("input[name=seconds]").fill("3")
    expect(setup.locator("[data-summary-duration]")).to_have_text("0:03")
    setup.get_by_role("button", name="Démarrer").click()
    expect(admin.get_by_text("Compte à rebours démarré.")).to_be_visible()
    expect(screen.locator(".screen-timer [data-timer-clock]")).to_have_text(
        re.compile(r"^\+0:0\d$"), timeout=6000
    )
    expect(screen.locator(".screen-timer.is-overtime [data-timer-over]")).to_have_text(
        "Temps écoulé"
    )
    check_layout(screen, "overtime-past-agenda")

    # Locking entries hides the password and the QR code (FR-27), and says so.
    admin.locator("#status-bar").get_by_role("button", name="Verrouiller les entrées").click()
    expect(
        admin.get_by_text("Entrées verrouillées\u00a0: plus personne ne peut rejoindre.")
    ).to_be_visible()
    expect(screen.locator("[data-entries]")).to_have_text("Entrées fermées")
    expect(screen.locator("[data-password]")).to_have_count(0)
    expect(screen.locator(".join-qr")).to_have_count(0)
    expect(screen.locator(".screen-foot")).to_contain_text(f"/{public_id}")
    assert password not in screen.content()
    expect(screen.locator(".screen-timer")).to_have_class(re.compile(r"\bis-large\b"))
    check_layout(screen, "locked")

    # Hiding the chronometer: the agenda takes the stage.
    admin.locator("#timer").get_by_role("button", name="Retirer de l'écran").click()
    expect(screen.locator(".screen-timer")).to_have_count(0)
    expect(screen.locator(".screen-agenda.is-large")).to_be_visible()

    # A new screen link, in the Projection screen rubric: the open screen stops working at
    # once (FR-16).
    show_rubric(admin, "screen")
    admin.get_by_role("button", name="Nouveau lien d'écran").click()
    expect(admin.get_by_text("Nouveau lien d'écran créé")).to_be_visible()
    expect(screen.get_by_role("heading", name="Page introuvable")).to_be_visible()
    new_url = admin.locator("#links-screen-url").input_value()
    assert new_url != screen_url
    screen.goto(new_url)
    expect(screen.locator(".screen-agenda")).to_be_visible()

    # Only the expected 404s of the old link; no CSP violation, no script error.
    assert errors.unexpected("404") == []


def test_reduced_motion_keeps_the_type_sizes(
    desktop: BrowserContext,
    browser: Browser,
    live_server: str,
    errors: ErrorWatch,
    world: World,
    owner: User,
) -> None:
    """A projector computer set to reduce motion fits the boxes as any other: a transition,
    even a tiny one, would let the screen measure the old type size and shrink every box."""
    public_id = world.assembly(owner, title="AG de l'association")
    with world.database.write() as db:
        assembly = assemblies.get_assembly(db, owner, public_id)
        assembly.agenda = [{"title": title} for title in AGENDA]
        assembly.agenda_current = 2
    world.poll(owner, public_id)
    screen_url = console(desktop, errors, owner, public_id).locator("#links-screen-url")
    url = screen_url.input_value()
    levels: dict[str, list[str | None]] = {}
    for motion in ("no-preference", "reduce"):
        context = browser.new_context(
            base_url=live_server, viewport={"width": 1920, "height": 1080}, reduced_motion=motion
        )
        page = errors.watch(context.new_page())
        page.goto(url)
        expect(page.get_by_role("heading", name="Approbation des comptes")).to_be_visible()
        page.wait_for_timeout(300)
        levels[motion] = page.evaluate(
            "[...document.querySelectorAll('[data-fit]')]"
            ".map(box => box.getAttribute('data-fit-level'))"
        )
        context.close()
    assert levels["no-preference"]  # the screen has boxes to fit
    assert levels["reduce"] == levels["no-preference"]
    assert errors.unexpected() == []


def test_what_the_room_sees_follows_and_acts_in_place(
    app: FastAPI, desktop: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """The list of "Ce que la salle voit" follows what other consoles change (a poll closed, the
    chronometer started), and its buttons act without reloading the page, the focus staying on
    the button pressed."""
    from agaura.web.realtime import manage_channel
    from tests.web.e2e.test_editor import organizer
    from tests.web.screen.conftest import screen_url

    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, PollInput("Coll", "single_choice", ["Oui", "Non"]))
    for token in world.voters(owner, public_id, 3):
        world.vote(public_id, token, poll_id, {"choice": 1})
    admin = organizer(desktop, errors, owner, public_id)
    show_rubric(admin, "screen")
    card = admin.locator("#screen-now")
    expect(card).to_contain_text("1 vote en cours")
    admin.evaluate("window.notReloaded = true")

    # Closed and the chronometer started from another console, which notifies this one.
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
        timer.start(db, owner, public_id, duration=None, show=True)
    app.state.hub.publish(manage_channel(public_id), "turnout", "polls")
    show = card.get_by_role("button", name="Afficher\u00a0: résultat de «\u00a0Coll\u00a0»")
    expect(show).to_be_visible(timeout=8000)
    expect(card).to_contain_text("Chronomètre\u00a0: Chronomètre, en marche")
    show.click()
    hide = card.get_by_role("button", name="Masquer\u00a0: résultat de «\u00a0Coll\u00a0»")
    expect(hide).to_be_focused()
    expect(card).to_contain_text("Résultat affiché\u00a0: Coll, Oui")
    projector = errors.watch(desktop.new_page())
    projector.goto(screen_url(world, owner, public_id))
    expect(projector.locator(f'[data-result="{poll_id}"]')).to_be_visible()
    hide.click()
    expect(
        card.get_by_role("button", name="Afficher\u00a0: résultat de «\u00a0Coll\u00a0»")
    ).to_be_focused()
    expect(projector.locator(f'[data-result="{poll_id}"]')).to_have_count(0)
    assert admin.evaluate("window.notReloaded === true")
    assert errors.unexpected() == []


BUSY_AGENDA = [
    "Accueil et vérification du quorum",
    "Rapport moral",
    "Rapport financier 2025",
    "Élection au conseil d'administration",
    "Projets à financer",
    "Lieu de la prochaine assemblée",
    "Questions diverses",
]


def test_a_busy_stage_stays_readable(projector: BrowserContext, world: World, owner: User) -> None:
    """Two polls open, a result shown with its count of votes, a question already put to the
    vote, the chronometer and the agenda (FR-73): the three cards share one row, the strip of
    questions is as tall as its line, no card falls to a small type, and a poll's description
    keeps its lines instead of being squashed."""
    from agaura.services import access
    from tests.web.screen.conftest import screen_url

    public_id = world.assembly(owner)
    vote_and_close(
        world, owner, public_id, PollInput("Essai", "single_choice", ["Oui", "Non"]), [1]
    )
    shown = vote_and_close(world, owner, public_id, RESOLUTION, [1] * 6 + [2] * 4 + [None])
    with world.database.write() as db:
        polls.show_on_screen(db, owner, shown, True)
        assembly = assemblies.get_assembly(db, owner, public_id)
        assembly.agenda = [{"title": title} for title in BUSY_AGENDA]
        assembly.agenda_current = 4
        timer.start(db, owner, public_id, duration=120, label="Réponse du trésorier", show=True)
        access.set_entries_locked(db, owner, public_id, True)
    world.poll(
        owner,
        public_id,
        PollInput(
            "Élection au conseil d'administration (2 sièges)",
            "approval",
            ["Alice Martin", "Bruno Petit", "Chloé Durand", "David Leroy"],
            {"seats": 2, "max_choices": 2},
        ),
    )
    world.poll(
        owner,
        public_id,
        PollInput(
            "Lieu de la prochaine assemblée",
            "single_choice",
            ["Salle des fêtes", "Maison des associations", "Médiathèque"],
            description="Pour 120 personnes.\nAccès aux personnes à mobilité réduite.",
        ),
    )
    page = projector.new_page()
    page.goto(screen_url(world, owner, public_id))
    for width, height in ((1920, 1080), (1280, 720)):
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(400)
        expect(page.locator(".screen-items.columns-3 > *")).to_have_count(3)
        levels = page.locator(".screen-items > [data-fit]").evaluate_all(
            "boxes => boxes.map(box => Number(box.getAttribute('data-fit-level')))"
        )
        assert max(levels) <= 1, (width, levels)
        strip = page.locator(".screen-past.is-strip").bounding_box()
        assert strip is not None and strip["height"] < height * 0.2, (width, strip)
        lines = page.locator(".poll-description").evaluate(
            """el => {
                const style = getComputedStyle(el);
                const line = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.2;
                return el.clientHeight / line;
            }"""
        )
        assert lines >= 1.9, (width, lines)  # its two lines, never a sliver
        assert page.evaluate("document.documentElement.scrollHeight") <= height


def test_the_way_to_join_leaves_room_beside_it(
    projector: BrowserContext, world: World, owner: User
) -> None:
    """A poll open while entries are still open, with the chronometer and the agenda (FR-70,
    FR-73): the way to join takes one row of the side column, its QR code at the left of the
    address and the password and no sentence under it; the chronometer and the agenda share the
    rest of the column, and every text of the way to join stays readable (16 px at 1920x1080,
    the same share of the height at 1280x720)."""
    from tests.web.screen.conftest import screen_url

    public_id = world.assembly(owner)
    with world.database.write() as db:
        assembly = assemblies.get_assembly(db, owner, public_id)
        assembly.agenda = [{"title": title} for title in AGENDA]
        assembly.agenda_current = 2
        timer.start(db, owner, public_id, duration=180, label="Temps de parole", show=True)
    world.poll(owner, public_id)
    page = projector.new_page()
    page.goto(screen_url(world, owner, public_id))
    join = page.locator(".screen-side .screen-join")
    expect(join).to_have_class(re.compile(r"\bis-row\b"))
    expect(join.locator(".join-hint")).to_have_count(0)
    expect(page.locator(".screen-agenda .agenda-list li")).to_have_count(5)
    for width, height in ((1920, 1080), (1280, 720)):
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(400)
        side = page.locator(".screen-side").bounding_box()
        box = join.bounding_box()
        qr = join.locator(".join-qr").bounding_box()
        text = join.locator(".join-text").bounding_box()
        agenda = page.locator(".screen-agenda").bounding_box()
        assert side and box and qr and text and agenda
        assert qr["x"] + qr["width"] <= text["x"], (width, qr, text)  # beside, not above
        assert box["height"] <= side["height"] * 0.3, (width, box, side)
        assert agenda["height"] >= box["height"] * 1.5, (width, agenda, box)
        sizes = join.locator(".join-text > *").evaluate_all(
            "els => els.map(el => parseFloat(getComputedStyle(el).fontSize))"
        )
        assert min(sizes) >= 16 * height / 1080, (width, sizes)
        assert page.evaluate("document.documentElement.scrollHeight") <= height
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(SHOTS) / f"e2e-join-row-{width}.png"))
