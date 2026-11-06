"""Tribune and agenda run from a phone: fill the tribune row by row with the "+" button (in place
with meeting.js, and by a round trip without JavaScript), type the agenda in its text area (in
place with agenda.js, following the other consoles, and by a round trip without JavaScript),
then move through it during the session. Set ``AGAURA_SHOTS_DIR`` to keep screenshots."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, expect

from agaura.models import User
from agaura.services import assemblies, delegates, meeting, notes
from tests.conftest import World
from tests.web.e2e.conftest import ErrorWatch, new_phone, show_rubric
from tests.web.e2e.test_journeys import log_in

pytestmark = pytest.mark.e2e

SHOTS = os.environ.get("AGAURA_SHOTS_DIR")


def shot(page: Page, name: str, selector: str | None = None) -> None:
    if not SHOTS:
        return
    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    path = Path(SHOTS) / f"meeting-{name}.png"
    if selector:
        page.locator(selector).screenshot(path=path)
    else:
        page.screenshot(path=path)


def no_horizontal_scroll(page: Page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def current_item(world: World, owner: User, public_id: str) -> int | None:
    with world.database.read() as db:
        return assemblies.get_assembly(db, owner, public_id).agenda_current


def meeting_titles(world: World, owner: User, public_id: str) -> list[str]:
    with world.database.read() as db:
        return meeting.agenda_titles(assemblies.get_assembly(db, owner, public_id))


def test_tribune_and_agenda_from_a_phone(
    phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    page = errors.watch(phone.new_page())
    log_in(page, owner.email)
    page.goto(f"/manage/{public_id}")

    # Tribune, in Meeting (D-B): one row to start with, no suggested function; "+" adds a row
    # in place.
    show_rubric(page, "meeting")
    tribune = page.locator("#tribune")
    expect(tribune.get_by_text("Personne à la tribune pour l'instant.")).to_be_visible()
    rows = tribune.locator(".tribune-row")
    expect(rows).to_have_count(1)
    assert tribune.locator("input[placeholder]").count() == 0
    add = tribune.get_by_role("button", name="Ajouter une personne")
    box = add.bounding_box()
    assert box is not None and box["height"] >= 48 and box["width"] >= 48
    page.locator("#tribune-name-1").fill("Marie Dupont")
    page.locator("#tribune-role-1").fill("Présidente de séance")
    add.click()
    expect(rows).to_have_count(2)
    expect(page.locator("#tribune-name-2")).to_be_focused()
    assert page.url.endswith(f"/manage/{public_id}#tab-meeting")  # no round trip
    page.keyboard.type("Paul Martin")
    page.locator("#tribune-role-2").fill("Secrétaire")
    add.click()
    page.locator("#tribune-role-3").fill("Scrutatrice")  # name forgotten
    add.click()
    expect(page.locator("#tribune-name-4")).to_be_focused()
    expect(page.get_by_text("Personne 4", exact=True)).to_be_attached()  # legend, numbered
    shot(page, "tribune-adding", "#tribune")
    page.keyboard.type("Hugo Petit")
    page.keyboard.press("Enter")  # Enter saves: "Save" is the form's first button
    message = tribune.get_by_text("Indiquez le nom de cette personne, ou videz sa fonction.")
    expect(message).to_be_visible()
    expect(page.locator("#tribune-name-3")).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#tribune-role-3")).to_have_value("Scrutatrice")
    expect(rows).to_have_count(4)
    no_horizontal_scroll(page)
    shot(page, "tribune-error")
    page.locator("#tribune-name-3").fill("Léa Bernard")
    page.get_by_role("button", name="Enregistrer la tribune").click()
    expect(page.get_by_text("Tribune enregistrée.")).to_be_attached()
    names = page.locator(".tribune-list li")
    expect(names).to_have_text(
        [
            "Marie Dupont — Présidente de séance",
            "Paul Martin — Secrétaire",
            "Léa Bernard — Scrutatrice",
            "Hugo Petit",
        ]
    )
    shot(page, "tribune", "#tribune")

    # Agenda typed straight in the card's text area, numbering pasted from a document removed,
    # saved in place: the page does not reload.
    show_rubric(page, "agenda")
    agenda = page.locator("#agenda")
    field = agenda.get_by_label("Points de l'ordre du jour")
    unsaved = agenda.get_by_text("Modifications non enregistrées")
    discard = agenda.get_by_role("button", name="Annuler les modifications")
    save = agenda.get_by_role("button", name="Enregistrer l'ordre du jour")
    expect(unsaved).to_be_hidden()
    expect(discard).to_be_hidden()
    page.evaluate("window.notReloaded = true")
    field.fill(
        "1. Approbation du procès-verbal\n2. Rapport moral\n\n3. Rapport financier\n4. Questions"
    )
    expect(unsaved).to_be_visible()
    save.click()
    saved = "Approbation du procès-verbal\nRapport moral\nRapport financier\nQuestions"
    expect(
        agenda.get_by_role("status").filter(has_text="Ordre du jour enregistré.")
    ).to_be_visible()
    expect(field).to_have_value(saved)
    expect(unsaved).to_be_hidden()
    choice = agenda.get_by_label("Point en cours")
    expect(choice).to_have_value("none")
    expect(choice.locator("option")).to_have_text(
        [
            "Aucun point en cours",
            "1. Approbation du procès-verbal",
            "2. Rapport moral",
            "3. Rapport financier",
            "4. Questions",
        ]
    )
    assert page.evaluate("window.notReloaded === true")
    # Small: the text area, one button, the choice of the item.
    box = agenda.bounding_box()
    assert box is not None and box["height"] < 450, box
    no_horizontal_scroll(page)
    shot(page, "agenda", "#agenda")

    # Refused on the field, the text kept; the changes discarded, the saved agenda is back.
    field.fill(saved + "\n" + "x" * 201)
    save.click()
    expect(agenda.get_by_role("alert")).to_have_text(
        "Le point 5 dépasse 200 caractères\u00a0: raccourcissez-le."
    )
    expect(field).to_have_attribute("aria-invalid", "true")
    expect(field).to_be_focused()
    expect(field).to_have_value(saved + "\n" + "x" * 201)
    expect(unsaved).to_be_visible()
    shot(page, "agenda-error", "#agenda")
    discard.click()
    expect(field).to_have_value(saved)
    expect(agenda.get_by_role("alert")).to_have_count(0)
    expect(field).not_to_have_attribute("aria-invalid", "true")
    expect(unsaved).to_be_hidden()
    assert page.evaluate("window.notReloaded === true")
    assert meeting_titles(world, owner, public_id) == saved.split("\n")

    # Any item, or none, chosen from the card, in place; Steering follows.
    change = agenda.get_by_role("button", name="Changer")
    box = change.bounding_box()
    assert box is not None and box["height"] >= 48
    choice.select_option("4")
    change.click()
    expect(page.locator("#agenda-now")).to_contain_text("Point 4 sur 4")
    assert current_item(world, owner, public_id) == 4
    expect(change).to_be_focused()
    choice.select_option("none")
    change.click()
    expect(page.locator("#agenda-now")).to_contain_text("Aucun point en cours")
    assert current_item(world, owner, public_id) is None
    expect(choice).to_have_value("none")
    assert page.url.endswith("#tab-agenda") and page.evaluate("window.notReloaded === true")

    # The same control in Steering, which stays in view after each move.
    show_rubric(page, "cockpit")
    now = page.locator("#agenda-now")
    expect(now).to_contain_text("Aucun point en cours")
    expect(now).to_contain_text("À venir\u00a0: Approbation du procès-verbal")
    now.get_by_role("button", name="Suivant").click()
    expect(page.locator("#agenda-now")).to_contain_text("Point 1 sur 4")
    expect(page.locator("#agenda-now")).to_contain_text("Approbation du procès-verbal")
    assert page.url.endswith("#tab-cockpit")
    expect(page.locator("#tab-cockpit")).to_be_visible()
    assert current_item(world, owner, public_id) == 1
    no_horizontal_scroll(page)
    shot(page, "console")
    show_rubric(page, "agenda")
    expect(choice).to_have_value("1")  # the card follows Steering

    # The session log, in Settings, records each change, never the names (drawn with the page:
    # the changes made in place show after a reload).
    page.reload()
    show_rubric(page, "settings")
    log = page.locator("#log")
    log.locator("summary").click()
    expect(log).to_contain_text("Tribune modifiée")
    expect(log).to_contain_text("Ordre du jour modifié")
    expect(log).to_contain_text("Point en cours de l'ordre du jour changé")
    expect(log).not_to_contain_text("Dupont")
    with world.database.read() as db:
        stored = assemblies.get_assembly(db, owner, public_id)
        assert [member["name"] for member in stored.tribune][:2] == ["Marie Dupont", "Paul Martin"]
        assert meeting.agenda_titles(stored)[0] == "Approbation du procès-verbal"
    assert errors.unexpected("status of 400") == []  # the refused tribune and agenda, by design


def test_add_a_person_without_javascript(
    browser: Browser, pw: Playwright, live_server: str, world: World, owner: User
) -> None:
    """Without JavaScript, "+" sends the form back with one more row, values kept, nothing
    saved."""
    public_id = world.assembly(owner)
    context = new_phone(browser, pw, live_server, java_script_enabled=False)
    try:
        page = context.new_page()
        log_in(page, owner.email)
        page.goto(f"/manage/{public_id}")
        page.locator("#tribune-name-1").fill("Marie Dupont")
        page.get_by_role("button", name="Ajouter une personne").click()
        expect(page.locator("#tribune .tribune-row")).to_have_count(2)
        assert page.url.endswith(f"/manage/{public_id}/tribune#tribune")
        expect(page.locator("#tribune-name-1")).to_have_value("Marie Dupont")
        with world.database.read() as db:
            assert assemblies.get_assembly(db, owner, public_id).tribune == []
        page.locator("#tribune-name-2").fill("Paul Martin")
        page.get_by_role("button", name="Enregistrer la tribune").click()
        expect(page.locator(".tribune-list li")).to_have_text(["Marie Dupont", "Paul Martin"])
        shot(page, "tribune-no-js", "#tribune")
    finally:
        context.close()


def test_the_agenda_follows_the_other_consoles(
    browser: Browser,
    pw: Playwright,
    live_server: str,
    desktop: BrowserContext,
    errors: ErrorWatch,
    world: World,
    owner: User,
) -> None:
    """A delegate edits the agenda from a phone: the owner's card follows without reloading,
    unless a text is being typed there, which keeps its value and the focus, with a notice;
    discarded, the card takes the other version. Unsaved changes survive a reload."""
    public_id = world.assembly(owner)
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, ["Accueil", "Rapport moral"])
        _, token = delegates.create_delegate(
            db, world.keyring, owner, public_id, "Marie, présidente", ["agenda"]
        )
    admin = errors.watch(desktop.new_page())
    log_in(admin, owner.email)
    admin.goto(f"/manage/{public_id}#agenda")
    mine = admin.locator("#agenda")
    field = mine.get_by_label("Points de l'ordre du jour")
    notice = mine.locator("[data-agenda-elsewhere]")
    admin.evaluate("window.notReloaded = true")
    context = new_phone(browser, pw, live_server)
    try:
        phone = errors.watch(context.new_page())
        phone.goto(f"/manage/{public_id}/delegate#k={token}")
        expect(phone.get_by_text("Accès délégué\u00a0: Marie, présidente")).to_be_visible()
        show_rubric(phone, "agenda")
        theirs = phone.locator("#agenda")
        their_field = theirs.get_by_label("Points de l'ordre du jour")
        their_save = theirs.get_by_role("button", name="Enregistrer l'ordre du jour")

        # Untouched here: the card takes the new agenda and its list of items.
        their_field.fill("Accueil\nRapport moral\nBudget")
        their_save.click()
        expect(field).to_have_value("Accueil\nRapport moral\nBudget", timeout=5000)
        expect(mine.get_by_label("Point en cours").locator("option")).to_have_count(4)
        expect(notice).to_have_text("")

        # Being typed here: kept, with the focus, and the notice; the item still follows.
        field.click()
        field.press("Control+End")
        admin.keyboard.type("\nQuestions diverses")
        typed = "Accueil\nRapport moral\nBudget\nQuestions diverses"
        their_field.fill("Accueil\nRapport moral\nBudget 2027")
        their_save.click()
        theirs.get_by_label("Point en cours").select_option("3")
        theirs.get_by_role("button", name="Changer").click()
        expect(mine.get_by_label("Point en cours")).to_have_value("3", timeout=5000)
        expect(notice).to_have_text(
            "L'ordre du jour vient d'être modifié depuis une autre console\u00a0: enregistrer ce"
            " texte remplacera cette version."
        )
        expect(field).to_have_value(typed)
        expect(field).to_be_focused()
        expect(mine.get_by_text("Modifications non enregistrées")).to_be_visible()
        expect(mine.get_by_label("Point en cours").locator("option").nth(3)).to_have_text(
            "3. Budget 2027"
        )
        shot(admin, "agenda-elsewhere", "#agenda")

        # Discarded: the card takes the version saved on the phone.
        mine.get_by_role("button", name="Annuler les modifications").click()
        expect(field).to_have_value("Accueil\nRapport moral\nBudget 2027")
        expect(notice).to_have_text("")
        assert admin.evaluate("window.notReloaded === true")
        assert phone.evaluate("document.activeElement.textContent.trim()") == "Changer"
    finally:
        context.close()

    # Unsaved changes survive a reload of the console, marked, until saved.
    field.fill("Accueil\nRapport moral\nBudget 2027\nClôture")
    admin.reload()
    show_rubric(admin, "agenda")
    expect(field).to_have_value("Accueil\nRapport moral\nBudget 2027\nClôture")
    expect(mine.get_by_text("Modifications non enregistrées")).to_be_visible()
    mine.get_by_role("button", name="Enregistrer l'ordre du jour").click()
    expect(mine.get_by_text("Modifications non enregistrées")).to_be_hidden()
    expect(mine.get_by_role("status").filter(has_text="Ordre du jour enregistré.")).to_be_visible()
    admin.reload()
    expect(field).to_have_value("Accueil\nRapport moral\nBudget 2027\nClôture")
    expect(mine.get_by_text("Modifications non enregistrées")).to_be_hidden()
    with world.database.read() as db:
        stored = assemblies.get_assembly(db, owner, public_id)
        assert meeting.agenda_titles(stored)[-1] == "Clôture" and stored.agenda_current == 3
    assert errors.unexpected() == []


def test_the_agenda_without_javascript(
    browser: Browser, pw: Playwright, live_server: str, world: World, owner: User
) -> None:
    """Without JavaScript the same card posts and comes back to itself: the agenda saved, a
    refused text kept on the field with its message, the item chosen from the list."""
    public_id = world.assembly(owner)
    context = new_phone(browser, pw, live_server, java_script_enabled=False)
    try:
        page = context.new_page()
        log_in(page, owner.email)
        page.goto(f"/manage/{public_id}#agenda")
        agenda = page.locator("#agenda")
        field = agenda.get_by_label("Points de l'ordre du jour")
        field.fill("- Accueil\n- Rapport moral\n- " + "x" * 201)
        agenda.get_by_role("button", name="Enregistrer l'ordre du jour").click()
        assert page.url.endswith(f"/manage/{public_id}/agenda#agenda")
        expect(agenda.get_by_role("alert")).to_contain_text("Le point 3 dépasse 200 caractères")
        expect(field).to_have_attribute("aria-invalid", "true")
        expect(field).to_have_value("- Accueil\n- Rapport moral\n- " + "x" * 201)
        expect(agenda.get_by_text("Modifications non enregistrées")).to_be_hidden()
        field.fill("- Accueil\n- Rapport moral")
        agenda.get_by_role("button", name="Enregistrer l'ordre du jour").click()
        expect(page.get_by_text("Ordre du jour enregistré.")).to_be_visible()
        expect(field).to_have_value("Accueil\nRapport moral")
        agenda.get_by_label("Point en cours").select_option("2")
        agenda.get_by_role("button", name="Changer").click()
        expect(page.get_by_text("Point en cours modifié.")).to_be_visible()
        assert page.url.endswith("#agenda")
        expect(agenda.get_by_label("Point en cours")).to_have_value("2")
        assert current_item(world, owner, public_id) == 2
        shot(page, "agenda-no-js", "#agenda")
    finally:
        context.close()


def test_attendance_record_on_a_phone(
    phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """The headcount is saved from a phone, never blocks, and each reading shows in the access
    card, most recent first."""
    public_id = world.assembly(owner)
    world.voters(owner, public_id, 3)
    page = errors.watch(phone.new_page())
    log_in(page, owner.email)
    page.goto(f"/manage/{public_id}")
    # The command of Steering during the session (D-A); its card stays in Access.
    page.locator("#cmd-presence input[name=headcount]").fill("45")
    page.locator("#cmd-presence form[action*='/headcount'] button").click()
    expect(page.locator("[role=status].alert-notice")).to_contain_text("Effectif enregistré")
    world.poll(owner, public_id)
    world.voters(owner, public_id, 2)
    page.reload()
    show_rubric(page, "access")
    record = page.locator("#attendance")
    record.locator("summary").click()
    rows = record.locator("tbody tr")
    expect(rows).to_have_count(3)
    expect(rows.nth(0)).to_contain_text("Vote n° 1 lancé")
    expect(rows.nth(1)).to_contain_text("Effectif saisi")
    expect(rows.nth(1).locator("td").nth(2)).to_have_text("45")
    expect(rows.nth(1).locator("td").nth(3)).to_have_text("3")
    no_horizontal_scroll(page)
    record.scroll_into_view_if_needed()
    shot(page, "attendance", "#access")
    expect(page.locator("#presence input[name=headcount]")).to_have_value("45")  # one value
    assert errors.unexpected() == []


def test_notes_on_a_phone(
    phone: BrowserContext, desktop: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """Notes typed on a phone are added without reloading, survive a reload while unsent, and
    show up on another console; Ctrl+Enter adds from a computer; the quick note of Steering
    feeds the same list; deleting asks first."""
    public_id = world.assembly(owner)
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, ["Ouverture", "Rapport moral"])
        meeting.set_current_item(db, owner, public_id, 2)
    page = errors.watch(phone.new_page())
    page.on("dialog", lambda dialog: dialog.accept())
    log_in(page, owner.email)
    page.goto(f"/manage/{public_id}#notes")
    card = page.locator("#notes")
    expect(card).to_contain_text("Rattachée au point en cours\u00a0: 2. Rapport moral")
    other = errors.watch(desktop.new_page())
    log_in(other, owner.email)
    other.goto(f"/manage/{public_id}#notes")

    text = page.locator("#note-text")
    text.fill("Le rapport moral est présenté par la présidente.\nAucune objection.")
    page.evaluate("window.notReloaded = true")
    card.get_by_role("button", name="Ajouter").click()
    first = card.locator(".note").first
    expect(first).to_contain_text("Aucune objection.")
    expect(first).to_contain_text("2. Rapport moral")
    expect(text).to_have_value("")
    expect(text).to_be_focused()
    assert page.evaluate("window.notReloaded === true")  # no reload
    # The other console follows without reloading.
    expect(other.locator("#notes .note").first).to_contain_text("Aucune objection.", timeout=5000)

    # An unsent note survives a reload of the console.
    text.fill("Question de M. Martin sur le budget")
    card.get_by_text("Note interne").first.click()
    page.reload()
    expect(page.locator("#note-text")).to_have_value("Question de M. Martin sur le budget")
    expect(page.locator("#notes input[name=internal]").first).to_be_checked()
    page.locator("#notes").get_by_role("button", name="Ajouter").click()
    expect(page.locator("#notes .note").first.locator(".badge")).to_have_text("Interne")
    no_horizontal_scroll(page)
    shot(page, "notes", "#notes")

    # Ctrl+Enter from a computer.
    other.locator("#note-text").fill("Pause de dix minutes.")
    other.locator("#note-text").press("Control+Enter")
    expect(other.locator("#notes .note").first).to_contain_text("Pause de dix minutes.")
    expect(page.locator("#notes .note").first).to_contain_text("Pause", timeout=5000)

    # The quick note of Steering (D-A): added without reload, it heads the list of Meeting
    # and becomes the last note of the command, on this console and on the other one.
    show_rubric(page, "cockpit")
    page.evaluate("window.notReloaded = true")
    quick = page.locator("#cmd-notes")
    page.locator("#cmd-note-text").fill("Vote du rapport moral annoncé.")
    quick.get_by_role("button", name="Ajouter").click()
    expect(quick.locator("[data-notes-last]")).to_contain_text("Vote du rapport moral annoncé.")
    expect(page.locator("#cmd-note-text")).to_have_value("")
    expect(page.locator("#notes .note").first).to_contain_text("Vote du rapport moral annoncé.")
    expect(other.locator("#cmd-notes [data-notes-last]")).to_contain_text(
        "Vote du rapport moral annoncé.", timeout=5000
    )
    assert page.evaluate("window.notReloaded === true")
    # Its unsent draft is its own: the card's text area stays empty after a reload.
    page.locator("#cmd-note-text").fill("Brouillon du pilotage")
    page.reload()
    expect(page.locator("#cmd-note-text")).to_have_value("Brouillon du pilotage")
    expect(page.locator("#note-text")).to_have_value("")
    page.locator("#cmd-note-text").fill("")  # the input event forgets the draft
    show_rubric(page, "notes")

    # Delete, after a confirmation, on the phone; the computer follows.
    note = page.locator("#notes .note", has_text="Pause de dix minutes.")
    note.locator("summary").click()
    shot(page, "notes-edit", "#notes")
    note.get_by_role("button", name="Supprimer la note").click()
    expect(page.locator("#notes .note", has_text="Pause")).to_have_count(0)
    expect(other.locator("#notes .note", has_text="Pause")).to_have_count(0, timeout=5000)
    with world.database.read() as db:
        assert len(notes.list_notes(db, owner, public_id)) == 3
    assert errors.unexpected() == []
