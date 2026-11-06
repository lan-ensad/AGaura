"""Delegate links in a browser (FR-92): the owner creates a link in Settings, or in place from
"Confier" of a card or command (D-G), a phone opens it through its fragment, runs the
chronometer and a poll, follows the console live, and loses access as soon as the owner revokes
the link; the cards handed over say so with a pill that leads to the link."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Browser, BrowserContext, Locator, Page, Playwright, expect

from agaura.models import User
from agaura.services import delegates, meeting
from tests.conftest import World
from tests.web.e2e.conftest import ErrorWatch, new_phone, show_rubric
from tests.web.e2e.test_journeys import console, log_in

pytestmark = pytest.mark.e2e


def create_link(admin: Page, name: str, rights: list[str]) -> str:
    """Create a link in Settings as the owner does, and read its address: the form of a new
    link shows from the start while there is no link, else after the "+" of the card's head,
    the focus in its first field."""
    show_rubric(admin, "settings")
    new = admin.locator("#delegates-new")
    if not new.is_visible():
        admin.get_by_role("link", name="Nouveau lien délégué").click()
        expect(new).to_be_visible()
        expect(admin.locator("#delegates-new-name")).to_be_focused()
    new.locator("#delegates-new-name").fill(name)
    for label in rights:
        new.get_by_label(label, exact=True).check()
    new.get_by_role("button", name="Créer le lien").click()
    expect(admin.get_by_text("Lien créé.")).to_be_visible()
    field = admin.locator(".delegate-link .link-url").last
    expect(field).to_be_visible()  # the new link opens with its QR code
    expect(admin.locator(".delegate-link .qr svg").last).to_be_visible()
    return field.input_value()


def places(card: Locator, toggle: Locator) -> list[float]:
    """The height of ``card`` and the box of its "Confier" toggle in the page, scrolling aside:
    what opening the toggle must leave as it is."""
    return card.evaluate(
        "(card, toggle) => { const c = card.getBoundingClientRect(),"
        " t = toggle.getBoundingClientRect();"
        " return [c.height, t.x + scrollX, t.y + scrollY, t.width, t.height]; }",
        toggle.element_handle(),
    )


def expanded(page: Page, selector: str) -> bool | None:
    """The expanded state that Chromium's accessibility tree gives the element of ``selector``
    (Playwright computes it on a details element, not on its summary)."""
    cdp = page.context.new_cdp_session(page)
    try:
        root = cdp.send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
        node = cdp.send("DOM.querySelector", {"nodeId": root, "selector": selector})["nodeId"]
        tree = cdp.send("Accessibility.getPartialAXTree", {"nodeId": node, "fetchRelatives": False})
    finally:
        cdp.detach()
    states = {prop["name"]: prop["value"]["value"] for prop in tree["nodes"][0]["properties"]}
    return states.get("expanded")


# Every card and command whose head hands a right over (HAND_OVER_CARDS), with its rubric.
HAND_OVER_PLACES = {
    "agenda-now": "cockpit",
    "ready": "cockpit",
    "cmd-notes": "cockpit",
    "cmd-timer": "cockpit",
    "cmd-presence": "cockpit",
    "drafts": "polls",
    "agenda": "agenda",
    "notes": "notes",
    "presence": "access",
    "screen-now": "screen",
    "timer": "meeting",
}


def test_a_phone_runs_part_of_the_console_through_a_link(
    desktop: BrowserContext,
    browser: Browser,
    pw: Playwright,
    live_server: str,
    errors: ErrorWatch,
    world: World,
    owner: User,
) -> None:
    public_id = world.assembly(owner, title="AG 2026")
    world.poll(owner, public_id, open_it=False)
    admin = console(desktop, errors, owner, public_id)
    url = create_link(admin, "Claire, secrétariat", ["Votes", "Chronomètre"])
    assert re.fullmatch(rf"{live_server}/manage/{public_id}/delegate#k=[\w-]{{43}}", url)

    context = new_phone(browser, pw, live_server)
    try:
        phone = errors.watch(context.new_page())
        phone.on("dialog", lambda dialog: dialog.accept())
        phone.goto(url)
        expect(phone).to_have_url(re.compile(rf"/manage/{public_id}$"))  # key gone from the bar
        expect(phone.get_by_text("Accès délégué\u00a0: Claire, secrétariat")).to_be_visible()
        tabs = phone.locator(".tab-bar > a")
        expect(tabs).to_have_count(3)  # Steering, Polls and Screen, no "More"
        expect(phone.locator(".tab-more")).to_have_count(0)
        assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

        # The chronometer, started from the phone's command, shows in the owner's status bar.
        phone.locator("#cmd-timer").get_by_role("button", name="Démarrer").click()
        expect(admin.locator(".status-bar .status-timer")).to_be_visible(timeout=5000)

        # A poll pushed from the phone shows in the owner's polls open now, in Steering and on
        # top of Polls; the phone, with the polls right, has them in Polls too.
        show_rubric(phone, "cockpit")
        phone.locator("#ready").get_by_role("button", name="Lancer le vote").click()
        expect(admin.locator("#live-panel .open-poll")).to_have_count(1, timeout=5000)
        expect(admin.locator("#polls-open .open-poll")).to_have_count(1)
        show_rubric(phone, "polls")
        expect(phone.locator("#polls-open")).to_be_visible()
        expect(phone.locator("#polls-open")).to_contain_text("Approbation des comptes")

        # Polls taken away, in the link's tile where everything is in view: the phone's
        # console redraws itself without them.
        item = admin.locator(".delegate-item", has_text="Claire, secrétariat")
        expect(item.locator("details")).to_have_count(0)
        expect(item.locator(".qr svg")).to_be_visible()
        item.get_by_label("Votes", exact=True).uncheck()
        item.get_by_role("button", name="Enregistrer").click()
        expect(admin.get_by_text("Accès du lien modifiés")).to_be_visible()
        expect(phone.locator(".tab-bar > a")).to_have_count(2, timeout=8000)
        expect(phone.locator("#tab-polls")).to_have_count(0)

        # Revoked: the phone turns into the page of an ended link by itself.
        show_rubric(admin, "settings")
        admin.locator(".delegate-item").get_by_role("button", name="Retirer le lien").click()
        expect(admin.get_by_text("Lien retiré")).to_be_visible()
        expect(phone.get_by_role("heading", name="Accès délégué terminé")).to_be_visible(
            timeout=8000
        )
        phone.get_by_role("button", name="L'effacer de cet appareil").click()
        expect(phone.get_by_text("Accès délégué effacé de cet appareil.")).to_be_visible()
    finally:
        context.close()
    with world.database.read() as db:
        assert delegates.list_delegates(db, owner, public_id) == []
    assert errors.unexpected("403") == []


def test_hand_over_the_chronometer_from_steering_on_a_phone(
    browser: Browser,
    pw: Playwright,
    live_server: str,
    errors: ErrorWatch,
    world: World,
    owner: User,
) -> None:
    """D-G as the owner changed it: on a phone, "Confier" in the head of the chronometer's
    command, a toggle, opens over the command and without leaving Steering a new link form with
    the chronometer ticked; the link created shows there (address, QR code) and in Settings,
    and the command then names its holder with a pill that leads to the link; "Fermer" gives
    the form back."""
    public_id = world.assembly(owner)
    owner_phone = new_phone(browser, pw, live_server)
    holder_phone = new_phone(browser, pw, live_server)
    try:
        admin = console(owner_phone, errors, owner, public_id)
        expect(admin.locator(".badge.delegate")).to_have_count(0)
        expect(admin.locator(".cockpit-foot")).to_have_count(0)
        command = admin.locator("#cmd-timer")
        toggle = command.locator(".hand-over > summary")
        expect(toggle).to_have_text("Confier")
        expect(toggle).to_have_attribute("aria-label", "Confier le chronomètre à une personne")
        panel = command.locator(".hand-over-panel")
        expect(panel).to_be_hidden()  # closed by default
        box = toggle.bounding_box()
        assert box is not None and box["height"] >= 24
        before = places(command, toggle)
        admin.evaluate("window.notReloaded = true")
        toggle.click()
        expect(panel).to_be_visible()
        # A toggle: the command keeps its size and the toggle its place, the panel floats over
        # what follows; a tap elsewhere keeps it open (its toggle, Escape or "Fermer" close it).
        assert places(command, toggle) == pytest.approx(before, abs=0.5)
        command.locator("h2").click()
        expect(panel).to_be_visible()
        expect(admin.locator("#cmd-timer-hand-timer")).to_be_checked()
        expect(panel.locator(".hand-over-more input")).to_have_count(5)  # the other rights
        expect(panel.locator(".hand-over-more input").first).to_be_hidden()  # folded
        admin.locator("#cmd-timer-hand-name").fill("Ali, scrutateur")
        panel.get_by_role("button", name="Créer le lien").click()
        url_field = admin.locator("#cmd-timer-hand-url")
        expect(url_field).to_be_visible()
        expect(url_field).to_be_focused()
        expect(command.locator(".hand-over-created .qr svg")).to_be_visible()
        assert admin.evaluate("window.notReloaded === true")  # in place, no new page
        expect(admin.locator("#tab-cockpit")).to_be_visible()
        url = url_field.input_value()
        assert re.fullmatch(rf"{live_server}/manage/{public_id}/delegate#k=[\w-]{{43}}", url)
        pill = command.locator(".badge.delegate")
        expect(pill).to_have_text("Confié à Ali, scrutateur")
        expect(admin.locator("#timer .badge.delegate")).to_have_text("Confié à Ali, scrutateur")
        assert admin.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        # Dismissed, the panel closes and gives the form back, for the next person.
        panel.get_by_role("button", name="Fermer").click()
        expect(panel).to_be_hidden()
        expect(toggle).to_be_focused()
        toggle.click()
        expect(admin.locator("#cmd-timer-hand-name")).to_have_value("")
        expect(panel.locator(".hand-over-created")).to_have_count(0)

        # In Settings too, where links are changed and revoked; the pill leads there.
        admin.keyboard.press("Escape")
        expect(panel).to_be_hidden()
        expect(toggle).to_be_focused()
        pill.click()
        expect(admin.locator("#tab-settings")).to_be_visible()
        item = admin.locator(".delegate-item", has_text="Ali, scrutateur")
        expect(item).to_be_in_viewport()
        expect(item.locator("input[name=rights]:checked")).to_have_count(1)
        expect(item.get_by_label("Chronomètre", exact=True)).to_be_checked()

        # The person's phone opens the link: the chronometer alone, its button primary.
        phone = errors.watch(holder_phone.new_page())
        phone.goto(url)
        expect(phone.get_by_text("Accès délégué\u00a0: Ali, scrutateur")).to_be_visible()
        expect(phone.locator(".tab-bar > a")).to_have_count(2)  # Steering and Screen
        expect(phone.locator("#cmd-timer .button.primary")).to_have_count(1)
        expect(phone.locator(".badge.delegate, .hand-over")).to_have_count(0)
        show_rubric(phone, "screen")
        expect(phone.locator("#timer")).to_be_visible()
        expect(phone.locator("#links")).to_have_count(0)  # never the screen link

        # Started from the person's phone: timer.js swaps the owner's command in place, the
        # pill stays.
        show_rubric(phone, "cockpit")
        show_rubric(admin, "cockpit")
        phone.locator("#cmd-timer").get_by_role("button", name="Démarrer").click()
        expect(admin.locator("#cmd-timer [data-timer-clock]")).to_be_visible(timeout=5000)
        expect(admin.locator("#cmd-timer").get_by_role("button", name="Pause")).to_be_visible()
        expect(admin.locator("#cmd-timer .button.primary")).to_have_count(0)
        expect(pill).to_have_text("Confié à Ali, scrutateur")
        assert admin.evaluate("window.notReloaded === true")
    finally:
        owner_phone.close()
        holder_phone.close()
    assert errors.unexpected() == []


@pytest.mark.parametrize("phone", [False, True], ids=["computer", "phone"])
def test_hand_over_is_a_stable_toggle(
    browser: Browser, live_server: str, errors: ErrorWatch, world: World, owner: User, phone: bool
) -> None:
    """The owner's request (2026-11-06): "Confier" is a toggle that neither resizes its card nor
    moves, in every card and command that hands a right over, at 1280 by 800 and on a 390 by
    844 phone, with a pill next to it or not. Open, it looks pressed and is exposed as
    expanded; its panel floats under it, over what follows, inside the window and above the
    tabs of a phone; a second press, or Escape from the keyboard, closes it."""
    public_id = world.assembly(owner)
    world.poll(owner, public_id, open_it=False)
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, ["Rapport moral", "Budget"])
        delegates.create_delegate(
            db, world.keyring, owner, public_id, "Paul Martin, secrétaire", ["timer", "polls"]
        )
    width, height = (390, 844) if phone else (1280, 800)
    context = browser.new_context(
        base_url=live_server,
        viewport={"width": width, "height": height},
        is_mobile=phone,
        has_touch=phone,
    )
    try:
        admin = console(context, errors, owner, public_id)
        for card_id, rubric in HAND_OVER_PLACES.items():
            show_rubric(admin, rubric)
            card = admin.locator(f"#{card_id}")
            summary = f"#{card_id}-hand > summary"
            toggle = admin.locator(summary)
            panel = admin.locator(f"#{card_id}-hand > .hand-over-panel")
            toggle.scroll_into_view_if_needed()
            closed = places(card, toggle)
            look = toggle.evaluate("el => getComputedStyle(el).backgroundColor")
            assert expanded(admin, summary) is False, card_id
            toggle.click()
            expect(panel).to_be_visible()
            assert places(card, toggle) == pytest.approx(closed, abs=0.5), card_id
            assert expanded(admin, summary) is True, card_id
            assert toggle.evaluate("el => getComputedStyle(el).backgroundColor") != look  # pressed
            under, box = toggle.bounding_box(), panel.bounding_box()
            assert under is not None and box is not None
            assert box["y"] >= under["y"] + under["height"], card_id
            assert box["x"] >= 0 and box["x"] + box["width"] <= width, card_id
            tabs = admin.locator(".tab-bar").bounding_box() if phone else None
            assert box["y"] + box["height"] <= (tabs["y"] if tabs else height), card_id
            on_top = panel.evaluate(
                "el => { const r = el.getBoundingClientRect();"
                " return el.contains(document.elementFromPoint(r.x + r.width / 2, r.bottom - 6)); }"
            )
            assert on_top, card_id  # over the cards that follow
            assert admin.evaluate("document.documentElement.scrollWidth") <= width, card_id
            toggle.click()
            expect(panel).to_be_hidden()
            assert expanded(admin, summary) is False, card_id
        # From the keyboard: Enter opens, Tab goes into the panel, Escape closes it and gives
        # the focus back to the toggle.
        toggle = admin.locator("#timer-hand > summary")
        toggle.focus()
        admin.keyboard.press("Enter")
        expect(admin.locator("#timer-hand > .hand-over-panel")).to_be_visible()
        admin.keyboard.press("Tab")
        expect(admin.locator("#timer-hand-name")).to_be_focused()
        admin.keyboard.press("Escape")
        expect(admin.locator("#timer-hand > .hand-over-panel")).to_be_hidden()
        expect(toggle).to_be_focused()
        assert errors.unexpected() == []
    finally:
        context.close()


def test_hand_over_without_script(
    browser: Browser, live_server: str, world: World, owner: User
) -> None:
    """Without script "Confier" is the same toggle, a native disclosure: open, its panel floats
    and the command keeps its size; the link created comes back in it (address, QR code), the
    form and "Fermer" hidden, and the toggle closes it."""
    public_id = world.assembly(owner)
    context = browser.new_context(
        base_url=live_server, viewport={"width": 1280, "height": 800}, java_script_enabled=False
    )
    try:
        page = context.new_page()
        log_in(page, owner.email)
        page.goto(f"/manage/{public_id}")
        command = page.locator("#cmd-timer")
        toggle = page.locator("#cmd-timer-hand > summary")
        panel = page.locator("#cmd-timer-hand > .hand-over-panel")
        before = places(command, toggle)
        toggle.click()
        expect(panel).to_be_visible()
        assert places(command, toggle) == pytest.approx(before, abs=0.5)
        page.locator("#cmd-timer-hand-name").fill("Ali, scrutateur")
        panel.get_by_role("button", name="Créer le lien").click()
        expect(page).to_have_url(re.compile(r"\?hand=cmd-timer&link=\d+#cmd-timer$"))
        expect(panel.locator(".hand-over-created .qr svg")).to_be_visible()
        expect(page.locator("#cmd-timer-hand-url")).to_be_visible()
        expect(page.locator("#cmd-timer-hand-name")).to_be_hidden()
        expect(panel.locator("[data-hand-over-close]")).to_be_hidden()
        toggle.click()
        expect(panel).to_be_hidden()
    finally:
        context.close()


@pytest.mark.parametrize("script", [True, False], ids=["script", "no-script"])
@pytest.mark.parametrize("phone", [False, True], ids=["computer", "phone"])
def test_settings_show_each_link_unfolded(
    browser: Browser,
    live_server: str,
    errors: ErrorWatch,
    world: World,
    owner: User,
    phone: bool,
    script: bool,
) -> None:
    """The owner's request (2026-11-06): in Settings each link shows its address, QR code,
    name and rights at once, nothing folded; the "+" of the card's head, an icon alone, shows
    the tile of a new link, with or without script, and "Annuler" folds it again; the page
    stays within a phone's width."""
    public_id = world.assembly(owner)
    with world.database.write() as db:
        for name, rights in (("Paul Martin, secrétaire", ["timer"]), ("Claire", ["agenda"])):
            delegates.create_delegate(db, world.keyring, owner, public_id, name, rights)
    width = 390 if phone else 1280
    context = browser.new_context(
        base_url=live_server,
        viewport={"width": width, "height": 844 if phone else 800},
        is_mobile=phone,
        has_touch=phone,
        java_script_enabled=script,
    )
    try:
        page = errors.watch(context.new_page())
        log_in(page, owner.email)
        page.goto(f"/manage/{public_id}#tab-settings")
        card = page.locator("#delegates")
        expect(card.locator(".delegate-item:not(.delegate-new)")).to_have_count(2)
        expect(card.locator(".delegate-item details")).to_have_count(0)
        for item in card.locator(".delegate-item:not(.delegate-new)").all():
            expect(item.locator(".qr svg")).to_be_visible()
            expect(item.locator(".link-url")).to_be_visible()
            expect(item.get_by_label("Nom", exact=True)).to_be_visible()
            expect(item.get_by_role("button", name="Enregistrer")).to_be_visible()
            expect(item.get_by_role("button", name="Retirer le lien")).to_be_visible()
        new = page.locator("#delegates-new")
        expect(new).to_be_hidden()
        add = card.locator(".card-head").get_by_role("link", name="Nouveau lien délégué")
        expect(add).to_have_text("")  # the icon alone, named by its label
        assert page.evaluate("document.documentElement.scrollWidth") <= width
        add.click()
        expect(new).to_be_visible()
        expect(new).to_be_in_viewport()
        if script:
            expect(page.locator("#delegates-new-name")).to_be_focused()
        assert page.evaluate("document.documentElement.scrollWidth") <= width
        new.get_by_role("link", name="Annuler").click()
        expect(new).to_be_hidden()
    finally:
        context.close()
    assert errors.unexpected() == []


@pytest.mark.parametrize("width", [320, 360])
def test_the_delegate_console_fits_small_phones(
    browser: Browser, live_server: str, errors: ErrorWatch, world: World, owner: User, width: int
) -> None:
    title = "Assemblée générale ordinaire de l'association des habitants du quartier"
    public_id = world.assembly(owner, title=title)
    world.poll(owner, public_id, open_it=False)
    with world.database.write() as db:
        _, token = delegates.create_delegate(
            db,
            world.keyring,
            owner,
            public_id,
            "Une personne au nom particulièrement long de la tribune",
            ["agenda", "notes", "polls", "timer", "headcount"],
        )
    context = browser.new_context(
        viewport={"width": width, "height": 740},
        is_mobile=True,
        has_touch=True,
        base_url=live_server,
    )
    try:
        page = errors.watch(context.new_page())
        page.goto(f"/manage/{public_id}/delegate#k={token}")
        expect(page).to_have_url(re.compile(rf"/manage/{public_id}$"))
        for rubric in ("cockpit", "polls", "agenda", "notes", "access", "screen"):
            page.goto(f"/manage/{public_id}#tab-{rubric}")
            expect(page.locator(f"#tab-{rubric}")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth") <= width, rubric
        assert errors.unexpected() == []
    finally:
        context.close()


def test_the_notes_pill_stays_while_notes_are_added(
    desktop: BrowserContext,
    browser: Browser,
    pw: Playwright,
    live_server: str,
    errors: ErrorWatch,
    world: World,
    owner: User,
) -> None:
    """In session, a link for the session notes, made in Settings, puts a pill on the quick
    note of Steering and on the notes card of Session notes, which adding notes (notes.js, no
    reload) keeps; the link opens the notes and nothing of the agenda."""
    public_id = world.assembly(owner)
    admin = console(desktop, errors, owner, public_id)
    url = create_link(admin, "Paul, secrétaire", ["Notes de séance"])
    show_rubric(admin, "cockpit")
    notes = admin.locator("#tab-cockpit #cmd-notes")
    pill = notes.locator(".cmd-head .badge.delegate")
    expect(pill).to_have_text("Confié à Paul, secrétaire")
    card_pill = admin.locator("#tab-notes #notes .card-head .badge.delegate")
    expect(card_pill).to_have_text("Confié à Paul, secrétaire")
    admin.evaluate("window.notReloaded = true")
    for text in ("Le quorum est atteint.", "Deuxième note."):
        notes.locator("#cmd-note-text").fill(text)
        notes.get_by_role("button", name="Ajouter").click()
        expect(notes.locator("[data-notes-last]")).to_contain_text(text)
    assert admin.evaluate("window.notReloaded === true")
    expect(pill).to_have_count(1)
    expect(pill).to_have_text("Confié à Paul, secrétaire")
    expect(card_pill).to_have_count(1)
    expect(admin.locator("#notes .note")).to_have_count(2)
    expect(admin.locator(".card-share")).to_have_count(0)

    context = new_phone(browser, pw, live_server)
    try:
        phone = errors.watch(context.new_page())
        phone.goto(url)
        expect(phone.get_by_text("Accès délégué\u00a0: Paul, secrétaire")).to_be_visible()
        expect(phone.locator(".tab-bar > a")).to_have_count(2)  # Steering and Meeting
        expect(phone.locator("#cmd-notes")).to_be_visible()
        expect(phone.locator("#agenda-now .agenda-step")).to_have_count(0)  # no item control
        expect(phone.locator(".badge.delegate")).to_have_count(0)
    finally:
        context.close()
    assert errors.unexpected() == []


def test_the_owner_follows_the_item_moved_from_a_phone(
    desktop: BrowserContext,
    browser: Browser,
    pw: Playwright,
    live_server: str,
    errors: ErrorWatch,
    world: World,
    owner: User,
) -> None:
    """A link with the agenda right moves the item under discussion from a phone: the owner's
    Steering shows it within 2 s without reloading, and the phone stays in place (FR-91)."""
    public_id = world.assembly(owner)
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, ["Rapport moral", "Budget"])
        _, token = delegates.create_delegate(
            db, world.keyring, owner, public_id, "Marie, présidente", ["agenda"]
        )
    admin = console(desktop, errors, owner, public_id)
    now = admin.locator("#agenda-now")
    expect(now).to_contain_text("À venir\u00a0: Rapport moral")
    admin.evaluate("window.notReloaded = true")
    context = new_phone(browser, pw, live_server)
    try:
        phone = errors.watch(context.new_page())
        phone.goto(f"/manage/{public_id}/delegate#k={token}")
        expect(phone.get_by_text("Accès délégué\u00a0: Marie, présidente")).to_be_visible()
        phone.evaluate("window.notReloaded = true")
        tap = phone.locator("#agenda-now").get_by_role("button", name="Suivant")
        tap.click()
        expect(now).to_contain_text("Point 1 sur 2 Rapport moral", timeout=2000)
        assert admin.evaluate("window.notReloaded === true")
        expect(phone.locator("#agenda-now")).to_contain_text("Point 1 sur 2 Rapport moral")
        expect(tap).to_be_focused()
        assert phone.evaluate("window.notReloaded === true")
        # And back from the owner's console: the phone follows too.
        now.get_by_role("button", name="Suivant").click()
        expect(phone.locator("#agenda-now")).to_contain_text("Point 2 sur 2 Budget", timeout=2000)
        assert phone.evaluate("window.notReloaded === true")
    finally:
        context.close()
    assert errors.unexpected() == []
