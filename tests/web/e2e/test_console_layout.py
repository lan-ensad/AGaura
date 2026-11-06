"""Session console layout in a browser: rubrics on a computer and on a phone, the status bar
kept in view and refreshed live, the page coming back to its rubric, no script."""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import time

import pytest
from fastapi import FastAPI
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, expect

from agaura.models import User
from agaura.services import assemblies, meeting, polls, timer
from tests.conftest import RESOLUTION, World
from tests.web.e2e.conftest import ErrorWatch, show_rubric, unfold
from tests.web.e2e.test_journeys import console, launch_poll, log_in

pytestmark = pytest.mark.e2e

OTHERS = ["polls", "meeting", "agenda", "notes", "access", "screen", "documents", "settings"]


def only_shown(page: Page, rubric: str) -> None:
    expect(page.locator(f"#tab-{rubric}")).to_be_visible()
    for other in ["cockpit", *OTHERS]:
        if other != rubric:
            expect(page.locator(f"#tab-{other}")).to_be_hidden()


def no_horizontal_scroll(page: Page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_rubrics_on_a_computer(
    desktop: BrowserContext, phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    # Two finished polls make Polls long enough to scroll; one draft to push.
    for title in ("Rapport moral", "Rapport financier"):
        finished = world.poll(owner, public_id, dataclasses.replace(RESOLUTION, title=title))
        with world.database.write() as db:
            polls.close_poll(db, owner, finished)
    world.poll(owner, public_id, open_it=False)
    page = console(desktop, errors, owner, public_id)
    only_shown(page, "cockpit")
    expect(page.locator(".tab-bar")).to_be_hidden()
    steering = page.locator(".rubric-nav a[href='#tab-cockpit']")
    expect(steering).to_have_attribute("aria-current", "page")

    for rubric in OTHERS:
        show_rubric(page, rubric)
        only_shown(page, rubric)
        assert page.url.endswith(f"#tab-{rubric}")
        link = page.locator(f".rubric-nav a[href='#tab-{rubric}']")
        expect(link).to_have_attribute("aria-current", "page")
        expect(steering).not_to_have_attribute("aria-current", "page")
        no_horizontal_scroll(page)

    # Back to the previous rubric with the browser's button.
    page.go_back()
    only_shown(page, "documents")

    # An address naming a card shows its rubric and opens it when it is folded.
    page.goto(f"/manage/{public_id}#attendance")
    only_shown(page, "access")
    expect(page.locator("#attendance")).to_have_attribute("open", "")

    # Polls: every poll a tile folded by default, unfolded by its summary.
    show_rubric(page, "polls")
    tiles = page.locator("#tab-polls details.poll-tile")
    expect(tiles.first).not_to_have_attribute("open", "")
    unfold(tiles)

    # The status bar stays in view while the rubric scrolls.
    page.mouse.wheel(0, 3000)
    page.wait_for_timeout(300)
    assert page.evaluate("window.scrollY") > 0
    box = page.locator("#status-bar").bounding_box()
    assert box is not None and -1 <= box["y"] <= 1

    # The "⋯" menu of a draft closes on a click elsewhere.
    menu = page.locator("#drafts details.menu")
    menu.locator("summary").click()
    expect(menu.get_by_role("button", name="Dupliquer")).to_be_visible()
    page.locator("#tab-polls-title").click()
    expect(menu).not_to_have_attribute("open", "")

    # A draft pushed from Polls: the page stays in Polls, the poll on top with its turnout; the
    # status bar and the card follow a phone joining without a reload.
    opened = page.locator("#polls-open")
    expect(opened).to_be_hidden()  # nothing open yet
    page.locator("#drafts").get_by_role("button", name="Lancer le vote").click()
    expect(page.get_by_text("Vote lancé.")).to_be_visible()
    only_shown(page, "polls")
    assert page.url.endswith("#tab-polls")
    expect(opened).to_be_visible()
    expect(opened.locator(".open-poll")).to_have_count(1)
    expect(opened).to_contain_text("Approbation des comptes")
    expect(opened).to_contain_text("0 bulletin / 0 présent")
    figures = page.locator("#status-bar .status-figures dd")
    expect(figures.nth(0)).to_have_text("0")
    # A single poll open: the ballots cast in it (R15).
    expect(page.locator("#status-bar")).to_contain_text(re.compile(r"Bulletins\s*0"))
    voter = errors.watch(phone.new_page())
    voter.goto(f"/{public_id}#k={world.password(owner, public_id)}")
    expect(voter.get_by_text("Approbation des comptes")).to_be_visible()
    expect(figures.nth(0)).to_have_text("1")
    expect(opened).to_contain_text("0 bulletin / 1 présent")
    only_shown(page, "polls")
    status_bar_on_one_line(page)
    assert errors.unexpected() == []


def test_the_status_bar_holds_on_one_line_at_1280(
    desktop: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """In session, with an attendee, the quorum reached, one poll open, entries open and a
    countdown running: state, figures, lock, chronometer and closing share one row (R15). A
    headcount saved adds a figure, and the bar may then wrap."""
    public_id = world.assembly(owner, quorum_rule="1")
    world.voters(owner, public_id, 1)
    world.poll(owner, public_id)
    with world.database.write() as db:
        timer.start(db, owner, public_id, duration=600, label="Temps de parole")
    page = console(desktop, errors, owner, public_id)
    expect(page.locator("#status-bar")).to_contain_text(re.compile(r"Bulletins\s*0"))
    expect(page.locator("#status-bar")).to_contain_text("atteint")
    expect(page.locator("#status-bar .status-timer svg.icon")).to_be_visible()
    status_bar_on_one_line(page)
    assert errors.unexpected() == []


def status_bar_on_one_line(page: Page) -> None:
    """At 1280 px the status bar holds on one line: state, figures, entries and their lock,
    the chronometer, closing the session; every item shares the first row."""
    items = page.locator("#status-bar .status-row > *").all()
    assert len(items) >= 4
    tops = [item.bounding_box() for item in items]
    assert all(box is not None for box in tops)
    first = min(box["y"] for box in tops if box)
    bottom = min(box["y"] + box["height"] for box in tops if box)
    assert all(box["y"] < bottom for box in tops if box), [
        (round(box["x"]), round(box["y"]), round(box["width"])) for box in tops if box
    ]
    assert first >= 0
    expect(
        page.locator("#status-bar").get_by_role("button", name="Terminer la session")
    ).to_be_visible()
    expect(page.locator(".session-end")).to_be_hidden()  # in the bar on a computer only


def test_polls_open_now_in_the_polls_rubric(
    desktop: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """A poll created on the spot and pushed from Polls stays in view there, closes from there,
    and the card follows what another console pushes and closes; pushing from Steering stays
    in Steering."""
    public_id = world.assembly(owner)
    world.poll(owner, public_id, open_it=False)
    page = console(desktop, errors, owner, public_id)
    show_rubric(page, "polls")
    opened = page.locator("#polls-open")
    expect(opened).to_be_hidden()

    # "Nouveau vote", the method first (D-C): a card, a title, "Lancer le vote".
    launch_poll(page, "Résolution", "Budget 2027", steering=False)
    only_shown(page, "polls")
    assert page.url.endswith("#tab-polls")
    expect(opened).to_be_visible()
    expect(opened.locator(".open-poll")).to_have_count(1)
    expect(opened).to_contain_text("Budget 2027")
    expect(opened).to_contain_text("0 bulletin / 0 présent")
    budget = opened.locator(".open-poll", has_text="Budget 2027")
    expect(budget).not_to_have_attribute("open", "")  # a tile, folded: title and state
    expect(budget.get_by_role("button", name="Clore le vote")).to_be_hidden()
    unfold(budget)

    # Pushed from the Steering of another console: in Steering there, here without a reload.
    other = errors.watch(desktop.new_page())
    other.goto(f"/manage/{public_id}")
    other.locator("#ready").get_by_role("button", name="Lancer le vote").click()
    expect(other.get_by_text("Vote lancé.")).to_be_visible()
    only_shown(other, "cockpit")
    expect(other.locator("#live-panel .open-poll")).to_have_count(2)
    expect(opened.locator(".open-poll")).to_have_count(2, timeout=5000)
    expect(opened).to_contain_text("Approbation des comptes")
    only_shown(page, "polls")
    expect(budget).to_have_attribute("open", "")  # redrawn live, still unfolded

    # Closed from Polls: to the results, still in Polls.
    opened.locator(".open-poll", has_text="Budget 2027").get_by_role(
        "button", name="Clore le vote"
    ).click()
    expect(page.get_by_text("Vote clos\u00a0; résultat calculé.")).to_be_visible()
    only_shown(page, "polls")
    expect(opened.locator(".open-poll")).to_have_count(1)
    expect(page.locator("#results")).to_contain_text("Budget 2027")

    # The link leads to the whole cockpit.
    opened.get_by_role("link", name="Aller dans Conduite").click()
    only_shown(page, "cockpit")
    expect(page.locator("#live-panel .open-poll")).to_have_count(1)

    # The last poll closed from another console: the card goes away by itself.
    show_rubric(page, "polls")
    expect(opened).to_be_visible()
    other.reload()
    other.locator("#live-panel").get_by_role("button", name="Clore le vote").click()
    expect(other.get_by_text("Vote clos\u00a0; résultat calculé.")).to_be_visible()
    expect(opened).to_be_hidden(timeout=5000)
    only_shown(page, "polls")
    assert errors.unexpected() == []


def test_another_console_opens_the_session(
    desktop: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """The rubrics depend on the state: a console left open redraws itself when another one
    opens or closes the session."""
    public_id = world.assembly(owner, in_session=False)
    page = console(desktop, errors, owner, public_id)
    expect(page.locator("#checklist")).to_be_visible()
    other = errors.watch(desktop.new_page())
    other.goto(f"/manage/{public_id}")
    other.get_by_role("button", name="Ouvrir la session").click()
    expect(other.get_by_text("La session est ouverte")).to_be_visible()
    expect(page.locator("#agenda-now")).to_be_visible(timeout=10_000)
    expect(page.locator("#status-bar")).to_contain_text("En cours")
    assert errors.unexpected() == []


def test_nothing_imposed_before_the_opening(
    desktop: BrowserContext, phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """In preparation there is no Steering: the console lands on Meeting, headed by a short
    recap of the steps whose lines lead to their cards; opened from the status bar, the
    session lands on Steering. On a phone, Séance leads the four tabs and "More"."""
    public_id = world.assembly(owner, in_session=False)
    page = console(desktop, errors, owner, public_id)
    only_shown(page, "meeting")
    expect(page.locator("#tab-cockpit")).to_have_count(0)
    expect(page.locator("a[href='#tab-cockpit']")).to_have_count(0)
    meeting = page.locator(".rubric-nav a[href='#tab-meeting']")
    expect(meeting).to_have_attribute("aria-current", "page")
    recap = page.locator("#checklist")
    expect(recap).to_be_in_viewport()
    expect(recap.locator(".check-step")).to_have_count(7)
    # One short line per step, in two columns at 1280 px: far smaller than a card per step
    # (the trial vote's line is as tall as its button).
    for line in recap.locator(".check-step a, .check-form").all():
        box = line.bounding_box()
        assert box is not None and box["height"] <= 44, box
    box = recap.bounding_box()
    assert box is not None and box["height"] <= 260, box
    no_horizontal_scroll(page)

    # A line leads to its card, in its rubric; the trial vote comes back to the recap.
    recap.get_by_role("link", name=re.compile("^Votes préparés")).click()
    only_shown(page, "polls")
    expect(page.locator("#drafts")).to_be_in_viewport()
    show_rubric(page, "meeting")
    recap.get_by_role("link", name=re.compile("^Ordre du jour")).click()
    only_shown(page, "agenda")
    expect(page.locator("#agenda")).to_be_in_viewport()
    show_rubric(page, "meeting")
    recap.get_by_role("button", name="Créer le vote d'essai").click()
    expect(page.get_by_text("Vote d'essai créé dans les brouillons")).to_be_visible()
    only_shown(page, "meeting")
    expect(recap.get_by_role("link", name=re.compile("^Vote d'essai"))).to_be_visible()

    # Opened from the status bar: Steering appears, and the console lands there.
    page.locator("#status-bar").get_by_role("button", name="Ouvrir la session").click()
    expect(page.get_by_text("La session est ouverte")).to_be_visible()
    only_shown(page, "cockpit")
    steering = page.locator(".rubric-nav a[href='#tab-cockpit']")
    expect(steering).to_have_attribute("aria-current", "page")
    expect(steering).to_contain_text("Conduite")
    expect(page.locator("#checklist")).to_have_count(0)

    # A phone in preparation: Séance, Votes, Ordre du jour, Accès and "Plus", of equal width.
    preparation = world.assembly(owner, in_session=False)
    mobile = console(phone, errors, owner, preparation)
    only_shown(mobile, "meeting")
    tabs = mobile.locator(".tab-bar > a, .tab-more > summary")
    expect(tabs).to_have_text(["Séance", "Votes", "Ordre du jour", "Accès", "Plus"])
    widths = [tab.bounding_box()["width"] for tab in tabs.all()]  # type: ignore[index]
    viewport = mobile.viewport_size
    assert viewport is not None and min(widths) >= viewport["width"] / 5 - 4, widths
    expect(mobile.locator(".tab-bar a[href='#tab-meeting']")).to_have_attribute(
        "aria-current", "page"
    )
    expect(mobile.locator("#checklist")).to_be_in_viewport()
    no_horizontal_scroll(mobile)
    assert errors.unexpected() == []


def test_rubrics_on_a_phone(
    phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    page = console(phone, errors, owner, public_id)
    expect(page.locator(".rubric-nav")).to_be_hidden()
    bar = page.locator(".tab-bar")
    expect(bar).to_be_visible()
    box = bar.bounding_box()
    viewport = page.viewport_size
    assert box is not None and viewport is not None
    assert abs(box["y"] + box["height"] - viewport["height"]) <= 1  # at the bottom
    for tab in bar.locator(":scope > a, .tab-more > summary").all():
        tab_box = tab.bounding_box()
        assert tab_box is not None and tab_box["height"] >= 48
    only_shown(page, "cockpit")
    expect(bar.locator("a[href='#tab-cockpit']")).to_have_attribute("aria-current", "page")

    bar.get_by_role("link", name="Votes").click()
    only_shown(page, "polls")
    expect(bar.locator("a[href='#tab-polls']")).to_have_attribute("aria-current", "page")

    # "More" holds the other five rubrics, and closes once one is chosen.
    more = page.locator(".tab-more")
    more.locator("summary").click()
    expect(more.get_by_role("link", name="Documents")).to_be_visible()
    # Every icon of the tabs and of "More" is drawn at 16 px at least (its box minus padding).
    drawn = bar.locator("svg.icon").evaluate_all(
        "icons => icons.map(el => { const s = getComputedStyle(el);"
        " return Math.min(el.clientWidth - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight),"
        " el.clientHeight - parseFloat(s.paddingTop) - parseFloat(s.paddingBottom)); })"
    )
    assert len(drawn) == 10 and min(drawn) >= 16, drawn
    more.get_by_role("link", name="Documents").click()
    only_shown(page, "documents")
    expect(more).not_to_have_attribute("open", "")
    expect(more).to_have_class("tab-more is-current")
    no_horizontal_scroll(page)
    for tab in bar.locator(":scope > a, .tab-more > summary").all():
        expect(tab.locator("svg.icon")).to_be_visible()  # the icon of the rubric, above its name

    # The end of a rubric is not hidden under the tabs.
    page.mouse.wheel(0, 5000)
    page.wait_for_timeout(300)
    footer = page.locator("footer.footer").bounding_box()
    bar_box = bar.bounding_box()
    assert footer is not None and bar_box is not None
    assert footer["y"] + footer["height"] <= bar_box["y"] + 1

    # Closing the session sits at the foot of Steering on a phone, not in the status bar.
    show_rubric(page, "cockpit")
    expect(
        page.locator("#status-bar").get_by_role("button", name="Terminer la session")
    ).to_be_hidden()
    page.locator(".session-end").get_by_role("button", name="Terminer la session").click()
    expect(page.get_by_role("heading", name="La session est terminée")).to_be_visible()
    assert errors.unexpected() == []


def test_console_without_javascript(
    browser: Browser, pw: Playwright, live_server: str, world: World, owner: User
) -> None:
    """Without script every rubric stays on the page, one after the other, and the forms of the
    checklist and of the commands work."""
    preparation = world.assembly(owner, in_session=False)
    public_id = world.assembly(owner)
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, ["Rapport moral", "Budget"])
    context = browser.new_context(
        base_url=live_server, viewport={"width": 1280, "height": 900}, java_script_enabled=False
    )
    try:
        page = context.new_page()
        log_in(page, owner.email)
        page.goto(f"/manage/{public_id}")
        for rubric in ["cockpit", *OTHERS]:
            expect(page.locator(f"#tab-{rubric}")).to_be_visible()
        page.locator(".rubric-nav a[href='#tab-settings']").click()
        assert page.url.endswith("#tab-settings")
        # Entries locked from their card (the same form as the status bar's), back to it.
        page.locator("#entries").get_by_role("button", name="Verrouiller les entrées").click()
        assert page.url.endswith("#entries")
        for place in ("#entries", "#status-bar"):
            unlock = page.locator(place).get_by_role("button", name="Déverrouiller les entrées")
            expect(unlock).to_be_visible()
        # The item under discussion moved from Steering: the form posts and comes back there.
        page.locator("#agenda-now").get_by_role("button", name="Suivant").click()
        assert page.url.endswith("#tab-cockpit")
        expect(page.get_by_text("Point en cours modifié.")).to_be_visible()
        expect(page.locator("#agenda-now")).to_contain_text("Point 1 sur 2 Rapport moral")
        # A quick note from Steering, then the chronometer started with the last settings.
        page.locator("#cmd-note-text").fill("Note sans script.")
        page.locator("#cmd-notes").get_by_role("button", name="Ajouter").click()
        assert page.url.endswith("#cmd-notes")
        expect(page.locator("#notes .note").first).to_contain_text("Note sans script.")
        page.locator("#cmd-timer").get_by_role("button", name="Démarrer").click()
        assert page.url.endswith("#cmd-timer")
        expect(page.locator("#cmd-timer").get_by_role("button", name="Pause")).to_be_visible()
        # Before the opening: no Steering, the checklist heads Meeting; its trial vote.
        page.goto(f"/manage/{preparation}")
        expect(page.locator("#tab-cockpit")).to_have_count(0)
        for rubric in OTHERS:
            expect(page.locator(f"#tab-{rubric}")).to_be_visible()
        expect(page.locator("#tab-meeting #checklist")).to_be_visible()
        page.get_by_role("button", name="Créer le vote d'essai").click()
        assert page.url.endswith("#checklist")
        expect(page.get_by_text("Vote d'essai créé dans les brouillons")).to_be_visible()
        expect(page.locator("#drafts .badge", has_text="Essai")).to_be_visible()
        # Opened from the status bar: back to Steering, there from now on.
        page.locator("#status-bar").get_by_role("button", name="Ouvrir la session").click()
        assert page.url.endswith("#tab-cockpit")
        expect(page.locator("#tab-cockpit #agenda-now")).to_be_visible()
    finally:
        context.close()


def test_steering_follows_other_consoles_and_keeps_the_focus(
    desktop: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """The item under discussion moves in place: "Suivant" keeps the focus once the live refresh
    has come back, and the card does not move; at the last item the focus stays in the card. A
    draft created in another console shows in "Prochain vote" without a reload (FR-91)."""
    public_id = world.assembly(owner)
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, ["Rapport moral", "Budget", "Questions"])
    page = console(desktop, errors, owner, public_id)
    now = page.locator("#agenda-now")
    ready = page.locator("#ready")
    expect(now).to_contain_text("À venir\u00a0: Rapport moral")
    expect(ready).to_contain_text("Aucun brouillon.")
    page.evaluate(
        "window.notReloaded = true; window.refreshes = 0;"
        "document.addEventListener('agaura:console-refreshed', () => { window.refreshes += 1; })"
    )
    box = now.bounding_box()
    next_button = now.get_by_role("button", name="Suivant")
    next_button.click()
    expect(now).to_contain_text("Point 1 sur 3 Rapport moral")
    expect(next_button).to_be_focused()
    # The console event of the tap brings a second refresh: nothing changed, nothing replaced.
    page.evaluate("document.activeElement.kept = true")  # a property: the HTML stays the same
    deadline = time.monotonic() + 5  # wait_for_function evaluates a string: refused by the CSP
    while page.evaluate("window.refreshes") < 2:
        assert time.monotonic() < deadline
        page.wait_for_timeout(50)
    expect(next_button).to_be_focused()
    assert page.evaluate("document.activeElement.kept === true")
    assert now.bounding_box() == box and page.evaluate("window.scrollY") == 0
    assert current_item(world, owner, public_id) == 1
    # At the last item "Suivant" is disabled: "Précédent", in the same form, keeps the focus.
    next_button.click()
    expect(now).to_contain_text("Point 2 sur 3 Budget")
    expect(next_button).to_be_focused()
    next_button.click()
    expect(now).to_contain_text("Point 3 sur 3 Questions")
    expect(next_button).to_be_disabled()
    expect(now.get_by_role("button", name="Précédent")).to_be_focused()

    # Drafts created in another console: in "Prochain vote", with the count in its head.
    other = errors.watch(desktop.new_page())
    for title in ("Budget 2027", "Cotisation 2027"):
        other.goto(f"/manage/{public_id}/polls/new?kind=resolution")
        other.locator("#editor-title").fill(title)
        other.get_by_role("button", name="Enregistrer le brouillon").click()
        expect(other.get_by_text("Vote créé en brouillon.")).to_be_visible()
        expect(ready.locator(".ready-item")).to_contain_text("Budget 2027", timeout=5000)
    expect(ready.locator(".cmd-head .to-home")).to_have_text("1 autre brouillon, dans Votes")
    expect(ready.get_by_role("button", name="Lancer le vote")).to_be_visible()
    assert page.evaluate("window.notReloaded === true")
    no_horizontal_scroll(page)
    assert errors.unexpected() == []


def current_item(world: World, owner: User, public_id: str) -> int | None:
    with world.database.read() as db:
        return assemblies.get_assembly(db, owner, public_id).agenda_current


def test_steering_commands_lead_to_their_cards(
    desktop: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """D-A: during the session each command of Steering leads to its card, which stays in its
    rubric; the cards keep their ids and the commands take the prefix cmd-."""
    public_id = world.assembly(owner)
    world.poll(owner, public_id, open_it=False)
    page = console(desktop, errors, owner, public_id)
    for command, link, rubric, card in (
        ("#agenda-now", "Tout l'ordre du jour", "agenda", "#agenda"),
        ("#cmd-notes", "Toutes les notes", "notes", "#notes"),
        ("#cmd-timer", "Réglages, dans Séance", "meeting", "#timer"),
        ("#cmd-presence", "Dans Accès", "access", "#presence"),
        ("#cmd-join", "Dans Accès", "access", "#join"),
        ("#ready", "Brouillons, dans Votes", "polls", "#drafts"),
    ):
        show_rubric(page, "cockpit")
        page.locator(command).get_by_role("link", name=link).click()
        only_shown(page, rubric)
        expect(page.locator(card)).to_be_in_viewport()
    no_horizontal_scroll(page)
    assert errors.unexpected() == []


@pytest.mark.parametrize("width", [320, 360])
def test_no_page_scrolls_sideways_on_small_phones(
    browser: Browser, live_server: str, errors: ErrorWatch, world: World, owner: User, width: int
) -> None:
    """Phones are 360 to 430 CSS pixels wide, 320 for the smallest: the dashboard, the account
    and every rubric of the console fit, with long titles and the sessions grouped by state."""
    title = "Assemblée générale ordinaire de l'association des habitants du quartier"
    public_id = world.assembly(
        owner, title=title, date=dt.date(2026, 6, 20), place="Salle des fêtes"
    )
    world.assembly(owner, in_session=False, title="Assemblée générale extraordinaire")
    world.poll(owner, public_id, open_it=False)
    context = browser.new_context(
        viewport={"width": width, "height": 740},
        is_mobile=True,
        has_touch=True,
        base_url=live_server,
    )
    try:
        page = errors.watch(context.new_page())
        log_in(page, owner.email)
        urls = ["/dashboard", "/account", f"/manage/{public_id}"]
        urls += [f"/manage/{public_id}#tab-{rubric}" for rubric in OTHERS]
        for url in urls:
            page.goto(url)
            expect(page.locator("main")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth") <= width, url
        # Each tab keeps its icon and label inside its own box (no overflow, no clipping).
        page.goto(f"/manage/{public_id}")
        tabs = page.locator(".tab-bar > a, .tab-more > summary")
        expect(tabs).to_have_count(5)
        for tab in tabs.all():
            fits = tab.evaluate(
                "el => el.scrollWidth <= el.clientWidth && [...el.children]"
                " .filter(child => child.getClientRects().length).every(child => {"
                " const box = child.getBoundingClientRect(), own = el.getBoundingClientRect();"
                " return box.left >= own.left - 0.5 && box.right <= own.right + 0.5; })"
            )
            assert fits, tab.inner_text()
        page.goto("/dashboard")
        # Grouped by state, one block per session: its state and counts under the title, and
        # the whole block one link (the title's, stretched over it).
        expect(page.locator(".session-group h3")).to_have_text(["En cours", "En préparation"])
        row = page.locator(".session-row").first
        for part in (".badge", ".session-figures"):
            inner = row.locator(part).bounding_box()
            title = row.locator("a").bounding_box()
            assert inner is not None and title is not None
            assert inner["y"] >= title["y"] + title["height"] - 1, part
            assert inner["x"] + inner["width"] <= width, part
        figures = row.locator(".session-figures").bounding_box()
        assert figures is not None
        page.mouse.click(figures["x"] + 4, figures["y"] + figures["height"] / 2)
        expect(page).to_have_url(re.compile(rf"/manage/{public_id}$"))
        assert errors.unexpected() == []
    finally:
        context.close()


@pytest.mark.parametrize("width", [320, 1280])
def test_the_chronometer_fits_its_card(
    browser: Browser, live_server: str, errors: ErrorWatch, world: World, owner: User, width: int
) -> None:
    """The longest times (a 10-hour countdown, an hour of overtime) fit the card with a margin,
    in the cockpit and in the Meeting rubric."""
    in_session = world.assembly(owner)
    preparation = world.assembly(owner, in_session=False)
    context = browser.new_context(viewport={"width": width, "height": 900}, base_url=live_server)
    try:
        page = errors.watch(context.new_page())
        log_in(page, owner.email)
        for public_id in (in_session, preparation):
            for duration, elapsed in ((36000, 0), (60, 3700)):
                with world.database.write() as db:
                    timer.start(db, owner, public_id, duration=duration, label="Temps de parole")
                    assembly = assemblies.get_assembly(db, owner, public_id)
                    assert assembly.timer_started_at is not None
                    assembly.timer_started_at -= dt.timedelta(seconds=elapsed)
                page.goto(f"/manage/{public_id}?t={duration}#timer")
                clock = page.locator("#timer .timer-face-clock")
                expect(clock).to_have_text(re.compile(r"^\+?\d{1,2}:\d{2}:\d{2}$"))
                text, box = clock.evaluate(
                    "el => { const range = document.createRange(); range.selectNodeContents(el);"
                    " return [range.getBoundingClientRect().width, el.clientWidth]; }"
                )
                assert text <= 0.8 * box, (public_id, duration, text, box)
                if public_id == in_session:  # the command of Steering, too
                    page.goto(f"/manage/{public_id}?t={duration}#cmd-timer")
                    command = page.locator("#cmd-timer")
                    expect(command.locator(".clock")).to_have_text(re.compile(r"\d:\d{2}:\d{2}$"))
                    assert command.evaluate("el => el.scrollWidth <= el.clientWidth")
        assert errors.unexpected() == []
    finally:
        context.close()


def test_a_poll_closed_by_its_timer_is_announced_on_an_open_console(
    app: FastAPI, desktop: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """The background job closes a poll whose timer ran out and notifies the consoles: the
    polls open now follow, and the hint to reload the page for its result shows (FR-33)."""
    from agaura import tasks
    from agaura.models import Poll, utcnow
    from tests.web.e2e.test_editor import organizer

    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id)
    admin = organizer(desktop, errors, owner, public_id)
    show_rubric(admin, "cockpit")
    hint = admin.locator("#refresh-hint")
    expect(hint).to_be_hidden()
    with world.database.write() as db:
        db.get_one(Poll, poll_id).closes_at = utcnow() - dt.timedelta(seconds=1)
    assert tasks.close_expired_polls(world.database, app.state.hub) == 1
    expect(hint).to_be_visible()
    expect(hint).to_contain_text("Un vote vient d'être clos.")
    expect(admin.locator("#live-panel")).to_contain_text("Aucun vote en cours.")
    assert errors.unexpected() == []
