"""Main journeys in a browser: the organizer on a computer, voters on phones."""

from __future__ import annotations

import re
import time
from collections.abc import Sequence

import pytest
from fastapi import FastAPI
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, expect
from sqlalchemy import select

from agaura.config import SignupMode
from agaura.models import AccessMode, OutboxEmail, User
from agaura.services.polls import PollInput
from agaura.web.limits import UNKNOWN_IDS_PER_IP
from agaura.web.realtime import live_channel
from tests.conftest import World
from tests.web.conftest import PASSWORD
from tests.web.e2e.conftest import ErrorWatch, new_phone, show_rubric

pytestmark = pytest.mark.e2e


def log_in(page: Page, email: str) -> None:
    page.goto("/login")
    page.fill("#field-email", email)
    page.fill("#field-password", PASSWORD)
    page.locator("form[action='/login'] button[type=submit]").click()
    expect(page.get_by_text("Vous êtes connecté·e.")).to_be_visible()


def console(desktop: BrowserContext, errors: ErrorWatch, owner: User, public_id: str) -> Page:
    page = errors.watch(desktop.new_page())
    page.on("dialog", lambda dialog: dialog.accept())  # confirmations of the console
    log_in(page, owner.email)
    page.goto(f"/manage/{public_id}")
    return page


def launch_poll(
    admin: Page,
    method: str,
    title: str,
    options: Sequence[str] = (),
    fields: dict[str, str] | None = None,
    *,
    steering: bool = True,
) -> float:
    """Create a poll the method first and launch it at once (D-C): "Nouveau vote" in Polls, the
    card of ``method`` (step 1; "Choix unique" for a "Résolution", the form that card opens on,
    and its "Options libres" for a "Choix unique"), then the title, the ``options`` (one row
    each, rows added as needed) and ``fields`` (selector → value) at step 2, and "Lancer le
    vote". The console comes back to Polls, the poll on top among the polls open now; then, with
    ``steering``, to Steering, where the other steps of the journeys follow it. Returns the
    moment of the click on "Lancer le vote", for the delays measured from the push (NFR-03)."""
    card = "Choix unique" if method == "Résolution" else method
    show_rubric(admin, "polls")
    admin.locator("#drafts").get_by_role("link", name="Nouveau vote").click()
    admin.locator(".method-card").get_by_role("link", name=card, exact=True).click()
    expect(admin.locator(".editor-method .method-name")).to_contain_text(card)
    if method == "Choix unique":
        admin.get_by_role("button", name="Options libres").click()
        expect(admin.get_by_role("button", name="Options libres")).to_have_attribute(
            "aria-pressed", "true"
        )
    admin.locator("#editor-title").fill(title)
    rows = admin.locator("input[name=option_label]")
    for index, label in enumerate(options):
        if index >= rows.count():
            admin.get_by_role("button", name="Ajouter des options").click()
        rows.nth(index).fill(label)
    for selector, value in (fields or {}).items():
        admin.locator(selector).fill(value)
    clicked = time.monotonic()
    admin.locator(".editor-actions").get_by_role("button", name="Lancer le vote").click()
    expect(admin.get_by_text("Vote lancé.")).to_be_visible()
    expect(admin.locator("#polls-open")).to_be_visible()
    expect(admin.locator("#polls-open")).to_contain_text(title)
    if steering:
        show_rubric(admin, "cockpit")
        expect(admin.locator("#live-panel")).to_be_visible()
    return clicked


def test_full_session_with_qr_join_live_push_and_results(
    desktop: BrowserContext, phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    public_id = world.assembly(owner, in_session=False)
    admin = console(desktop, errors, owner, public_id)
    admin.get_by_role("button", name="Ouvrir la session").click()
    expect(admin.get_by_text("La session est ouverte")).to_be_visible()
    # Visible text, not only in the HTML: two groups of four, as on the projection screen, in
    # the "Rejoindre" command of Steering (its card stays in Access and attendance).
    expect(admin.locator("#cmd-join .secret span")).to_have_count(2)
    password = "".join(admin.locator("#cmd-join .secret").inner_text().split())
    assert re.fullmatch(r"\d{8}", password)

    # The QR code carries the password in the fragment: joining needs no typing (FR-22).
    voter = errors.watch(phone.new_page())
    voter.goto(f"/{public_id}#k={password}")
    expect(voter.get_by_text("En attente du prochain vote")).to_be_visible()
    assert "#k=" not in voter.url
    # Attendees, in the status bar shown on every rubric.
    expect(admin.locator("#status-bar .status-figures dd").first).to_have_text("1")

    # A pushed poll appears on the phone without reloading, within 2 s (NFR-03).
    started = launch_poll(admin, "Résolution", "Approbation des comptes 2025")
    expect(voter.get_by_role("heading", name="Approbation des comptes 2025")).to_be_visible(
        timeout=5000
    )
    assert time.monotonic() - started < 2.0

    voter.get_by_text("Pour", exact=True).click()
    voter.get_by_role("button", name="Continuer").click()
    voter.get_by_role("button", name="Confirmer mon vote").click()
    expect(voter.get_by_text("Vote enregistré")).to_be_visible()

    # The console turnout follows without reloading.
    expect(admin.locator("#live-panel")).to_contain_text("1 bulletin / 1 présent")
    admin.locator("#live-panel").get_by_role("button", name="Clore le vote").click()
    expect(admin.get_by_text("Vote clos\u00a0; résultat calculé.")).to_be_visible()
    expect(voter.get_by_role("heading", name="Approbation des comptes 2025")).to_be_hidden()
    # Closed from Steering: back there, the "Vote clos" command on top publishes (D-K).
    expect(admin.locator("#tab-cockpit")).to_be_visible()
    admin.locator("#just-closed").get_by_role("button", name="Publier le résultat").click()
    expect(voter.get_by_text("Adoptée")).to_be_visible()

    admin.get_by_role("button", name="Terminer la session").click()
    expect(voter.get_by_text("La session est terminée.")).to_be_visible(timeout=10_000)
    assert errors.unexpected("401") == []


def test_approval_and_majority_judgment_ballots(
    desktop: BrowserContext, phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    admin = console(desktop, errors, owner, public_id)
    voter = errors.watch(phone.new_page())
    voter.goto(f"/{public_id}#k={world.password(owner, public_id)}")
    expect(voter.get_by_text("En attente du prochain vote")).to_be_visible()

    launch_poll(
        admin,
        "Vote par approbation",
        "Conseil",
        ["Alice", "Bruno", "Chloé"],
        {"#editor-seats": "2"},
    )
    expect(voter.get_by_role("heading", name="Conseil")).to_be_visible()
    voter.get_by_text("Alice", exact=True).click()
    voter.get_by_text("Chloé", exact=True).click()
    expect(voter.locator("[data-counter]")).to_contain_text("Maximum atteint\u00a0: 2 choix sur 2.")
    voter.get_by_role("button", name="Continuer").click()
    voter.get_by_role("button", name="Confirmer mon vote").click()
    expect(voter.get_by_text("Vote enregistré")).to_be_visible()

    launch_poll(admin, "Jugement majoritaire", "Projets", ["Jardin", "Fresque"])
    # By its title: the confirmation of "Conseil" now names it too ("Vote suivant : Projets").
    section = voter.get_by_role("region", name="Projets")
    expect(section).to_be_visible()
    section.locator("fieldset", has_text="Jardin").get_by_text("Très bien").click()
    section.get_by_role("button", name="Continuer").click()
    summary = voter.get_by_role("region", name="Projets")
    expect(summary).to_contain_text("À rejeter")  # ungraded option gets the lowest grade
    summary.get_by_role("button", name="Confirmer mon vote").click()
    expect(voter.get_by_role("region", name="Projets")).to_contain_text("Vote enregistré")
    expect(admin.locator("#live-panel")).to_contain_text("1 bulletin")
    assert errors.unexpected() == []


def test_ballot_in_progress_survives_a_new_poll(
    app: FastAPI, phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    first = world.poll(owner, public_id, PollInput("Première", "single_choice", ["Oui", "Non"]))
    voter = errors.watch(phone.new_page())
    voter.goto(f"/{public_id}#k={world.password(owner, public_id)}")
    voter.get_by_text("Non", exact=True).click()
    world.poll(owner, public_id, PollInput("Seconde", "single_choice", ["Rouge", "Bleu"]))
    app.state.hub.publish(live_channel(public_id), "update", "polls")  # as the console does
    expect(voter.get_by_role("heading", name="Seconde")).to_be_visible()
    first_section = voter.locator(f"#poll-{first}")
    expect(first_section.get_by_label("Non")).to_be_checked()
    assert errors.unexpected() == []


def push(app: FastAPI, world: World, owner: User, public_id: str, poll: PollInput) -> int:
    """Open a poll and tell the live pages, as the console does."""
    poll_id = world.poll(owner, public_id, poll)
    app.state.hub.publish(live_channel(public_id), "update", "polls")
    return poll_id


def top_of(page: Page, selector: str) -> float:
    return float(page.locator(selector).evaluate("el => el.getBoundingClientRect().top"))


def test_blank_vote_is_one_exclusive_choice_and_ungraded_options_are_named(
    phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """SPEC §6.1, §6.4, §6.6: the blank comes after the options in every method, at their level
    and drawn like them, and excludes them; Majority Judgment counts the graded options and names
    the others before the deposit; the summary names the blank with the ballot's words."""
    public_id = world.assembly(owner)
    single = world.poll(owner, public_id)
    approval = world.poll(owner, public_id, PollInput("Conseil", "approval", ["Alice", "Bruno"]))
    options = ["Jardin", "Fresque murale", "Atelier vélo"]
    judgment = world.poll(owner, public_id, PollInput("Projets", "majority_judgment", options))
    voter = errors.watch(phone.new_page())
    voter.goto(f"/{public_id}#k={world.password(owner, public_id)}")

    resolution = voter.locator(f"#poll-{single}")
    expect(resolution.get_by_label("Abstention")).to_have_attribute("type", "radio")
    # One more row of the options' group: the same gap, size and frame as theirs.
    first, last, box = (
        resolution.get_by_text(name, exact=True).bounding_box()
        for name in ("Pour", "Contre", "Abstention")
    )
    assert first is not None and last is not None and box is not None
    assert box["y"] - last["y"] == last["y"] - first["y"]
    assert (box["x"], box["width"], box["height"]) == (last["x"], last["width"], last["height"])
    frame = (
        "el => ['borderStyle', 'borderColor', 'backgroundColor'].map(k => getComputedStyle(el)[k])"
    )
    abstention, against = (
        resolution.get_by_text(name, exact=True) for name in ("Abstention", "Contre")
    )
    assert abstention.evaluate(frame) == against.evaluate(frame)

    ballot = voter.locator(f"#poll-{approval}")
    blank = ballot.get_by_label("Vote blanc")
    ballot.get_by_text("Alice", exact=True).click()
    ballot.get_by_text("Bruno", exact=True).click()
    ballot.get_by_text("Vote blanc", exact=True).click()
    expect(blank).to_be_checked()
    for name in ("Alice", "Bruno"):
        expect(ballot.get_by_label(name)).not_to_be_checked()
    expect(ballot.locator("[data-counter]")).to_have_text("0 option cochée.")
    ballot.get_by_text("Bruno", exact=True).click()
    expect(blank).not_to_be_checked()

    grid = voter.locator(f"#poll-{judgment}")
    # One more button after the last option, the size of a grade's.
    pill, grade = (
        grid.locator(selector).bounding_box()
        for selector in (".mj-blank .grade-pill", ".mj-option >> nth=2 >> .grade-pill >> nth=0")
    )
    assert pill is not None and grade is not None
    assert (pill["x"], pill["width"], pill["height"]) == (
        grade["x"],
        grade["width"],
        grade["height"],
    )
    assert pill["y"] > grade["y"]
    counter = grid.locator("[data-graded]")
    expect(counter).to_have_attribute("role", "status")
    expect(counter).to_have_text("0 option sur 3 évaluée")
    grid.locator("fieldset", has_text="Jardin").get_by_text("Très bien").click()
    expect(counter).to_have_text("1 option sur 3 évaluée")
    grid.locator("fieldset", has_text="Fresque murale").get_by_text("Passable").click()
    expect(counter).to_have_text("2 options sur 3 évaluées")
    grid.get_by_text("Vote blanc", exact=True).click()
    expect(grid.locator("input[name^=grade-]:checked")).to_have_count(0)
    expect(counter).to_have_text("0 option sur 3 évaluée")
    grid.locator("fieldset", has_text="Atelier vélo").get_by_text("Bien", exact=True).click()
    expect(grid.get_by_label("Vote blanc")).not_to_be_checked()
    grid.get_by_role("button", name="Continuer").click()
    warning = grid.locator(".ungraded")
    expect(warning).to_have_text(
        "Jardin et Fresque murale n'ont pas de mention\u00a0: "
        "ces options compteront «\u00a0À rejeter\u00a0»."
    )
    expect(grid.get_by_role("button", name="Confirmer mon vote")).to_have_accessible_description(
        warning.inner_text()
    )
    grid.get_by_role("button", name="Modifier mon vote").click()
    grid.get_by_text("Vote blanc", exact=True).click()
    grid.get_by_role("button", name="Continuer").click()
    expect(grid.locator(".summary-main")).to_have_text("Vote blanc")
    expect(grid.locator(".ungraded")).to_have_count(0)
    assert errors.unexpected() == []


def test_summary_announcement_focus_and_reading_position(
    app: FastAPI, phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """FR-41 with several open polls: a summary of what remains, a recorded poll on one line,
    a new poll read out without moving the focus or what the voter reads."""
    public_id = world.assembly(owner, title="AG de printemps")
    many = [f"Personne candidate numéro {n}" for n in range(1, 9)]
    first = world.poll(owner, public_id, PollInput("Première question", "approval", many))
    second = world.poll(
        owner, public_id, PollInput("Seconde question", "single_choice", ["Oui", "Non"])
    )
    voter = errors.watch(phone.new_page())
    voter.goto(f"/{public_id}#k={world.password(owner, public_id)}")
    expect(voter.get_by_role("heading", level=1, name="AG de printemps")).to_be_visible()
    summary = voter.get_by_role("navigation", name="Votes ouverts")
    expect(summary).to_contain_text("À voter\u00a0: 2 · Enregistrés\u00a0: 0")
    announcer = voter.locator("[data-announce]")
    expect(announcer).to_have_attribute("aria-live", "polite")
    expect(announcer).to_have_text("")  # nothing read out on the first load

    # A link of the summary leads to its poll, below the summary that sticks to the top.
    summary.get_by_role("link", name="Première question").click()
    expect(voter).to_have_url(re.compile(f"#poll-{first}$"))
    heading = voter.locator(f"#poll-{first}-title")
    title_box, bar = heading.bounding_box(), summary.bounding_box()
    assert title_box is not None and bar is not None and bar["y"] == 0
    assert title_box["y"] >= bar["height"] - 1  # not hidden under it

    # Vote on the second poll: the full confirmation, focused; the summary follows at once.
    poll = voter.locator(f"#poll-{second}")
    poll.get_by_text("Oui", exact=True).click()
    poll.get_by_role("button", name="Continuer").click()
    poll.get_by_role("button", name="Confirmer mon vote").click()
    expect(poll.get_by_role("heading", name="Vote enregistré")).to_be_focused()
    expect(summary).to_contain_text("À voter\u00a0: 1 · Enregistrés\u00a0: 1")
    expect(summary.get_by_role("link")).to_have_count(1)

    # A poll pushed now is read out; the focus and the confirmation it sits in stay.
    third = push(
        app,
        world,
        owner,
        public_id,
        PollInput("Troisième question", "single_choice", ["Oui", "Non"]),
    )
    expect(announcer).to_have_text("Nouveau vote ouvert\u00a0: Troisième question")
    expect(voter.locator(f"#poll-{third}")).to_be_attached()
    expect(poll.get_by_role("heading", name="Vote enregistré")).to_be_focused()
    expect(summary).to_contain_text("À voter\u00a0: 2 · Enregistrés\u00a0: 1")

    # Once the focus is elsewhere, the next refresh folds the confirmation to one line.
    voter.locator(f"#poll-{third}").get_by_text("Non", exact=True).click()
    push(
        app,
        world,
        owner,
        public_id,
        PollInput("Quatrième question", "single_choice", ["Oui", "Non"]),
    )
    expect(announcer).to_have_text("Nouveau vote ouvert\u00a0: Quatrième question")
    expect(poll).to_have_class(re.compile(r"\bpoll-recorded\b"))
    expect(poll).to_contain_text("✓ Seconde question · vote enregistré")
    expect(voter.locator(f"#poll-{third}").get_by_label("Non")).to_be_checked()  # kept

    # Reading the first poll, further down: a poll pushed on top leaves it where it is.
    voter.locator(f"#poll-{first}").get_by_text(many[4], exact=True).scroll_into_view_if_needed()
    voter.evaluate(f"window.scrollBy(0, {top_of(voter, f'#poll-{first}')} - 300)")
    before = top_of(voter, f"#poll-{first}")
    assert top_of(voter, "#live") < 0 and abs(before - 300) < 2
    push(
        app,
        world,
        owner,
        public_id,
        PollInput("Cinquième question", "single_choice", ["Oui", "Non"]),
    )
    expect(announcer).to_have_text("Nouveau vote ouvert\u00a0: Cinquième question")
    assert abs(top_of(voter, f"#poll-{first}") - before) < 2
    assert voter.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert errors.unexpected() == []


def test_voter_page_at_320_pixels(
    browser: Browser, pw: Playwright, live_server: str, world: World, owner: User
) -> None:
    """The smallest phones: no sideways scroll with the summary, targets of 44 px or more."""
    public_id = world.assembly(
        owner, title="Assemblée générale ordinaire de l'association des habitants"
    )
    long = "Modification de l'article 12 des statuts relatif à la composition du bureau"
    world.poll(owner, public_id, PollInput(long, "approval", ["Pour la modification", "Contre"]))
    world.poll(
        owner, public_id, PollInput("Projets", "majority_judgment", ["Jardin partagé", "Fresque"])
    )
    world.poll(owner, public_id, PollInput(f"{long} (bis)", "single_choice", ["Oui", "Non"]))
    context = browser.new_context(
        viewport={"width": 320, "height": 640}, base_url=live_server, has_touch=True, is_mobile=True
    )
    errors = ErrorWatch()
    voter = errors.watch(context.new_page())
    voter.goto(f"/{public_id}#k={world.password(owner, public_id)}")
    summary = voter.get_by_role("navigation", name="Votes ouverts")
    expect(summary.get_by_role("link")).to_have_count(3)
    assert voter.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    for target in (
        *summary.get_by_role("link").all(),
        *voter.locator("label:has([data-blank])").all(),
    ):
        box = target.bounding_box()
        assert box is not None and box["height"] >= 44 and box["x"] >= 0
    voter.mouse.wheel(0, 900)
    assert voter.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    context.close()
    assert errors.unexpected() == []


def test_voting_without_javascript(
    browser: Browser, pw: Playwright, live_server: str, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    world.poll(owner, public_id)
    context = new_phone(browser, pw, live_server, java_script_enabled=False)
    voter = context.new_page()
    voter.goto(f"/{public_id}")
    password = world.password(owner, public_id)
    expect(voter.locator("#field-secret")).to_have_attribute("inputmode", "numeric")  # D10
    voter.fill("#field-secret", f"{password[:3]} {password[3:]}")
    voter.get_by_role("button", name="Rejoindre").click()
    voter.get_by_text("Contre", exact=True).click()
    voter.get_by_role("button", name="Continuer").click()
    voter.get_by_role("button", name="Confirmer mon vote").click()
    expect(voter.get_by_text("Vote enregistré").first).to_be_visible()

    # Without script nothing unchecks the options: the server refuses a blank with them.
    world.poll(owner, public_id, PollInput("Conseil", "approval", ["Alice", "Bruno"]))
    voter.goto(f"/{public_id}")
    ballot = voter.locator("section.poll", has_text="Conseil")
    ballot.get_by_text("Alice", exact=True).click()
    ballot.get_by_text("Vote blanc", exact=True).click()
    ballot.get_by_role("button", name="Continuer").click()
    expect(voter.get_by_role("alert")).to_contain_text("Un vote blanc ne se combine avec aucun")
    expect(voter.get_by_label("Alice")).to_be_checked()
    expect(voter.get_by_label("Vote blanc")).not_to_be_checked()
    context.close()


def test_individual_code_card(
    desktop: BrowserContext, phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    admin = console(desktop, errors, owner, public_id)
    show_rubric(admin, "access")
    admin.locator("input[name=count]").fill("2")
    admin.get_by_role("button", name="Générer et imprimer").click()
    expect(admin.locator(".code-card-code")).to_have_count(2)
    codes = admin.locator(".code-card-code").all_inner_texts()
    assert len(codes) == 2
    voter = errors.watch(phone.new_page())
    voter.goto(f"/{public_id}#k={codes[0]}")
    expect(voter.get_by_text("En attente du prochain vote")).to_be_visible()
    assert errors.unexpected() == []


def test_qr_join_from_an_address_scanning_session_ids(
    phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """Phones of a venue share one address: once it has asked for too many unknown session IDs,
    it gets a neutral join form, which still joins from the QR code (``agaura.web.limits``)."""
    public_id = world.assembly(owner, title="AG de quartier")
    for n in range(UNKNOWN_IDS_PER_IP[0]):
        assert phone.request.get(f"/{n:05d}A").status == 404
    neutral = errors.watch(phone.new_page())
    neutral.goto(f"/{public_id}")
    expect(neutral.get_by_label("Mot de passe de la session ou code personnel")).to_be_visible()
    expect(neutral.get_by_text("AG de quartier")).to_have_count(0)

    voter = errors.watch(phone.new_page())
    voter.goto(f"/{public_id}#k={world.password(owner, public_id)}")
    expect(voter.get_by_text("En attente du prochain vote")).to_be_visible()
    assert "#k=" not in voter.url
    assert errors.unexpected() == []


def test_signup_with_the_anti_bot_check(
    app: FastAPI, desktop: BrowserContext, errors: ErrorWatch, world: World
) -> None:
    # Signup is closed by default (accounts by invitation); this journey covers the open mode.
    app.state.settings = app.state.settings.model_copy(update={"signup": SignupMode.OPEN})
    page = errors.watch(desktop.new_page())
    page.goto("/signup")
    page.fill("#field-email", "nouvelle@example.org")
    page.fill("#field-password", PASSWORD)
    page.fill("#field-password_confirm", PASSWORD)
    submit = page.get_by_role("button", name="Créer le compte")
    expect(submit).to_be_enabled(timeout=15_000)  # the proof of work is solved in the page
    submit.click()
    expect(page).to_have_url(re.compile(r"/dashboard$"))
    expect(
        page.get_by_text("Confirmez votre adresse e-mail pour créer des sessions")
    ).to_be_visible()
    with world.database.read() as db:
        assert db.scalar(select(OutboxEmail.to_address)) == "nouvelle@example.org"
    assert errors.unexpected() == []
