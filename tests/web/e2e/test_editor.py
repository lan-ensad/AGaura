"""Poll creation journeys, the method first (D-C), on a phone: the organizer prepares, edits,
reorders and pushes polls from a phone; another phone votes. Without JavaScript too. Set
``AGAURA_SHOTS_DIR`` to keep screenshots."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, expect

from agaura.models import PollStatus, User
from agaura.services import polls
from agaura.services.polls import PollInput
from tests.conftest import RESOLUTION, World
from tests.web.e2e.conftest import ErrorWatch, new_phone, show_rubric, unfold
from tests.web.e2e.test_journeys import log_in

pytestmark = pytest.mark.e2e

SHOTS = os.environ.get("AGAURA_SHOTS_DIR")
PASTE = """(input, text) => {
  const data = new DataTransfer();
  data.setData("text/plain", text);
  const options = {clipboardData: data, bubbles: true, cancelable: true};
  input.dispatchEvent(new ClipboardEvent("paste", options));
}"""


def shot(page: Page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=Path(SHOTS) / f"{name}.png", full_page=True)


def organizer(context: BrowserContext, errors: ErrorWatch, owner: User, public_id: str) -> Page:
    page = errors.watch(context.new_page())
    page.on("dialog", lambda dialog: dialog.accept())
    log_in(page, owner.email)
    page.goto(f"/manage/{public_id}")
    return page


def draft_titles(page: Page) -> list[str]:
    return [t.strip() for t in page.locator("#drafts .poll-tile-title strong").all_inner_texts()]


def new_poll(page: Page, method: str) -> None:
    """From Polls, "Nouveau vote", then the card of ``method``: step 2. A "Résolution" is the
    form "Choix unique" opens on."""
    card = "Choix unique" if method == "Résolution" else method
    show_rubric(page, "polls")
    page.locator("#drafts").get_by_role("link", name="Nouveau vote").click()
    expect(page.get_by_role("heading", name="Choisir le mode de scrutin")).to_be_visible()
    page.locator(".method-card").get_by_role("link", name=card, exact=True).click()
    expect(page.locator(".editor-method .method-name")).to_contain_text(card)
    if method == "Résolution":
        expect(page.get_by_role("button", name="Résolution")).to_have_attribute(
            "aria-pressed", "true"
        )


def test_prepare_edit_reorder_and_push_from_a_phone(
    browser: Browser,
    pw: Playwright,
    live_server: str,
    phone: BrowserContext,
    errors: ErrorWatch,
    world: World,
    owner: User,
) -> None:
    past = world.assembly(owner, title="AG 2025")
    world.poll(owner, past, PollInput("Cotisation 2026", "single_choice", ["20 €", "25 €"]))
    public_id = world.assembly(owner, title="AG 2026")
    admin = organizer(phone, errors, owner, public_id)

    # Step 1: the methods as cards; step 2: the settings of a single choice only.
    show_rubric(admin, "polls")
    admin.locator("#drafts").get_by_role("link", name="Nouveau vote").click()
    expect(admin.get_by_role("heading", name="Nouveau vote")).to_be_visible()
    expect(admin.locator('.stepper [aria-current="step"]')).to_have_text("Mode de scrutin")
    expect(admin.locator(".method-card")).to_have_count(4)
    assert admin.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(admin, "editor-step1-phone")
    admin.locator(".method-card").get_by_role("link", name="Choix unique", exact=True).click()
    expect(admin.locator('.stepper [aria-current="step"]')).to_have_text("Question et réglages")
    # "Choix unique" opens on a resolution; "Options libres" switches in place.
    free = admin.get_by_role("button", name="Options libres")
    expect(admin.get_by_role("button", name="Résolution")).to_have_attribute("aria-pressed", "true")
    expect(free).to_have_attribute("aria-pressed", "false")
    free.click()
    expect(free).to_have_attribute("aria-pressed", "true")
    expect(free).to_be_focused()
    expect(admin.locator("input[name=seats]")).to_have_count(0)  # no setting of another method
    expect(admin.locator("input[name=blank_label]")).to_have_count(0)  # a fixed blank vote
    sentence = admin.locator("[data-rule-sentence]")
    expect(sentence).to_have_text(
        "L'option qui obtient le plus de voix l'emporte, si elle est seule en tête."
    )
    expect(admin.locator("#rule-edit")).not_to_have_attribute("open", "")
    admin.get_by_role("button", name="Enregistrer le brouillon").click()  # empty: refused
    expect(admin.get_by_text("Le vote n'a pas été enregistré")).to_be_visible()
    expect(admin.locator("#editor-title")).to_be_focused()

    expect(admin.locator("#error-options")).to_be_visible()
    admin.locator("#editor-title").fill("Lieu de la prochaine AG")
    expect(admin.locator("#error-title")).to_be_hidden()  # corrected: its message goes
    rows = admin.locator("input[name=option_label]")
    rows.nth(0).fill("Salle des fêtes")
    expect(admin.locator("#error-options")).to_be_hidden()
    expect(rows).to_have_count(2)  # two empty rows to begin with
    rows.nth(0).press("Enter")  # next row, not a submission
    expect(rows.nth(1)).to_be_focused()
    admin.keyboard.type("Médiathèque")
    # Two options: the rule of the engine is "more than half", said at once (§6.2).
    expect(sentence).to_have_text(
        "L'option qui obtient plus de la moitié des suffrages exprimés l'emporte."
    )
    rows.nth(1).press("Enter")  # at the last row: a row added
    expect(rows).to_have_count(3)
    expect(rows.nth(2)).to_be_focused()
    rows.nth(2).evaluate(PASTE, "Parc\nGymnase\nJardin\n")  # one row per pasted line
    expect(rows).to_have_count(5)
    assert rows.evaluate_all("inputs => inputs.map(input => input.value)") == [
        "Salle des fêtes",
        "Médiathèque",
        "Parc",
        "Gymnase",
        "Jardin",
    ]
    admin.get_by_role("button", name="Ajouter des options").click()
    expect(rows).to_have_count(6)
    expect(rows.nth(5)).to_be_focused()
    admin.get_by_role("button", name="Retirer l'option 6").click()
    admin.get_by_role("button", name="Retirer l'option 5").click()
    expect(rows).to_have_count(4)
    expect(rows.nth(3)).to_have_attribute("placeholder", "Option 4")
    admin.locator("input[name=option_description]").nth(0).fill("Centre-ville")
    # A rule refused: "Modifier la règle" opens again on the faulty field.
    admin.get_by_text("Modifier la règle").click()
    admin.locator("#threshold-percent").fill("150")  # selects "Autre seuil"
    expect(admin.locator("#threshold-custom")).to_be_checked()
    expect(sentence).to_have_text(
        "L'option qui obtient au moins … des suffrages exprimés l'emporte."
    )
    admin.get_by_role("button", name="Enregistrer le brouillon").click()
    expect(admin.locator("#rule-edit")).to_have_attribute("open", "")
    expect(admin.locator("#threshold-percent")).to_be_focused()
    expect(admin.locator("#error-threshold")).to_be_visible()
    admin.locator("label[for=threshold-majority]").click()
    expect(sentence).to_have_text(
        "L'option qui obtient plus de la moitié des suffrages exprimés l'emporte."
    )
    assert admin.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(admin, "editor-phone")
    admin.get_by_role("button", name="Enregistrer le brouillon").click()
    expect(admin.get_by_text("Vote créé en brouillon.")).to_be_visible()

    # A second poll: a resolution, its labels and rule as voters will see them.
    new_poll(admin, "Résolution")
    preview = admin.locator(".option-preview")
    expect(preview).to_contain_text("Pour")
    expect(preview.locator(".is-blank")).to_have_text("Abstention (vote blanc)")
    expect(admin.locator("input[name=option_label]").nth(0)).to_be_hidden()  # folded
    admin.get_by_text("Modifier les intitulés").click()
    admin.locator("input[name=option_label]").nth(0).fill("Oui")
    expect(preview.locator("li").first).to_have_text("Oui")
    expect(admin.locator("[data-rule-sentence]")).to_have_text(
        "Adoptée si «\u00a0Oui\u00a0» obtient plus de la moitié des suffrages exprimés."
    )
    admin.locator("input[name=option_label]").nth(0).fill("Pour")
    admin.get_by_text("Modifier la règle").click()
    admin.locator("label[for=threshold-two_thirds]").click()
    admin.locator("label[for=base-cast]").click()
    expect(admin.locator("[data-rule-sentence]")).to_have_text(
        "Adoptée si «\u00a0Pour\u00a0» obtient au moins les deux tiers des bulletins déposés."
    )
    admin.locator("#editor-title").fill("Approbation des comptes")
    admin.get_by_role("button", name="Enregistrer le brouillon").click()
    assert draft_titles(admin) == ["Lieu de la prochaine AG", "Approbation des comptes"]
    expect(admin.locator("#drafts")).to_contain_text("Résolution · Pour, Contre")

    # Edit the first draft: step 2 of its method, "Changer de mode" at hand.
    first = admin.locator("#drafts li").first
    unfold(first.locator("details.poll-tile"))  # a tile, folded: title and state
    first.locator("details.menu summary").click()  # during the session, in the "⋯" menu
    first.get_by_role("link", name="Modifier").click()
    expect(admin.locator("#editor-title")).to_have_value("Lieu de la prochaine AG")
    expect(admin.locator("#threshold-majority")).to_be_checked()
    admin.locator("#editor-title").fill("Lieu de l'AG 2027")
    admin.get_by_role("button", name="Enregistrer le brouillon").click()
    expect(admin.get_by_text("Brouillon enregistré.")).to_be_visible()

    # "Changer de mode" keeps the title and the options; "Annuler" saves nothing.
    item = admin.locator("#drafts li", has_text="Lieu de l'AG 2027")
    unfold(item.locator("details.poll-tile"))
    item.locator("details.menu summary").click()
    item.get_by_role("link", name="Modifier").click()
    admin.get_by_role("button", name="Changer de mode").click()
    expect(admin.locator('.stepper [aria-current="step"]')).to_have_text("Mode de scrutin")
    expect(admin.locator("#switch-single_choice")).to_be_checked()
    admin.locator("label[for=switch-majority_judgment]").click()
    expect(admin.locator("#switch-majority_judgment")).to_be_checked()
    shot(admin, "editor-change-phone")
    admin.get_by_role("button", name="Continuer").click()
    expect(admin.locator(".editor-method .method-name")).to_contain_text("Jugement majoritaire")
    expect(admin.locator("#editor-title")).to_have_value("Lieu de l'AG 2027")
    expect(admin.locator("input[name=option_label]").nth(3)).to_have_value("Gymnase")
    admin.locator(".editor-actions").get_by_role("link", name="Annuler").click()
    expect(admin.locator("#drafts")).to_contain_text("Choix unique · Salle des fêtes")

    # Reorder, then copy the polls of last year's session, from step 1.
    unfold(admin.locator("#drafts details.poll-tile", has_text="Approbation des comptes"))
    admin.get_by_role("button", name="Monter\u00a0: Approbation des comptes").click()
    expect(admin.get_by_text("Ordre des brouillons modifié.")).to_be_visible()
    assert draft_titles(admin) == ["Approbation des comptes", "Lieu de l'AG 2027"]
    show_rubric(admin, "polls")
    admin.locator("#drafts").get_by_role("link", name="Nouveau vote").click()
    admin.locator("#copy summary").click()
    admin.get_by_role("button", name="Copier en brouillons").click()
    expect(admin.get_by_text("Votes copiés en brouillons")).to_be_visible()
    assert draft_titles(admin)[-1] == "Cotisation 2026"
    shot(admin, "console-drafts-phone")

    # Push the edited draft with a timer; another phone votes on it.
    voter = errors.watch(new_phone(browser, pw, live_server).new_page())
    voter.goto(f"/{public_id}#k={world.password(owner, public_id)}")
    expect(voter.get_by_text("En attente du prochain vote")).to_be_visible()
    item = admin.locator("#drafts li", has_text="Lieu de l'AG 2027")
    unfold(item.locator("details.poll-tile"))
    menu = item.locator("details.menu")
    menu.locator("summary").click()  # the timer is in the "⋯" menu
    expect(menu.get_by_role("link", name="Modifier")).to_be_visible()
    admin.keyboard.press("Escape")
    expect(menu).not_to_have_attribute("open", "")
    menu.locator("summary").click()
    item.locator("input[name=minutes]").fill("10")
    assert admin.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(admin, "draft-menu-phone")
    item.get_by_role("button", name="Lancer avec cette minuterie").click()
    expect(admin.get_by_text("Vote lancé.")).to_be_visible()
    expect(voter.get_by_role("heading", name="Lieu de l'AG 2027")).to_be_visible()
    voter.get_by_text("Médiathèque", exact=True).click()
    voter.get_by_role("button", name="Continuer").click()
    voter.get_by_role("button", name="Confirmer mon vote").click()
    expect(voter.get_by_text("Vote enregistré")).to_be_visible()
    expect(admin.locator("#tab-polls")).to_be_visible()  # pushed from Polls: still there
    show_rubric(admin, "cockpit")
    expect(admin.locator("#live-panel")).to_contain_text("1 bulletin / 1 présent")

    with world.database.read() as db:
        listed = polls.list_polls(db, owner, public_id)
        pushed = listed[0]
        assert (pushed.title, pushed.status) == ("Lieu de l'AG 2027", PollStatus.OPEN)
        assert pushed.params == {"threshold": "majority"} and pushed.closes_at is not None
        assert [(o.label, o.description) for o in pushed.options] == [
            ("Salle des fêtes", "Centre-ville"),
            ("Médiathèque", None),
            ("Parc", None),
            ("Gymnase", None),
        ]
        resolution = next(poll for poll in listed if poll.title == "Approbation des comptes")
        assert resolution.params == {"base": "cast", "threshold": "two_thirds", "adopt_option": 1}
        assert resolution.blank_label == "Abstention"
    assert errors.unexpected("status of 400") == []


def test_editor_without_javascript(
    browser: Browser, pw: Playwright, live_server: str, world: World, owner: User
) -> None:
    """Step 1 links, step 2 a server-rendered form: a resolution created and launched during the
    session; then an election started as a single choice, its method changed on the way."""
    public_id = world.assembly(owner)
    world.poll(owner, public_id, RESOLUTION, open_it=False)
    context = new_phone(browser, pw, live_server, java_script_enabled=False)
    page = context.new_page()
    log_in(page, owner.email)

    # A resolution: a card, a title, "Lancer le vote" (§4.4).
    page.goto(f"/manage/{public_id}")
    page.locator("#drafts").get_by_role("link", name="Nouveau vote").click()
    page.locator(".method-card").get_by_role("link", name="Choix unique", exact=True).click()
    expect(page.get_by_role("button", name="Résolution")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("[data-rule-sentence]")).to_have_text(
        "Adoptée si «\u00a0Pour\u00a0» obtient plus de la moitié des suffrages exprimés."
    )
    page.get_by_text("Modifier les intitulés").click()  # a <details>: no script needed
    expect(page.locator("input[name=option_label]").nth(1)).to_have_value("Contre")
    page.locator("#editor-title").fill("Approbation des comptes 2025")
    page.locator("#editor-minutes").fill("5")
    shot(page, "editor-nojs-resolution-phone")
    page.locator(".editor-actions").get_by_role("button", name="Lancer le vote").click()
    expect(page.get_by_text("Vote lancé.")).to_be_visible()
    assert page.url.endswith(f"/manage/{public_id}#tab-polls")
    expect(page.locator("#polls-open")).to_contain_text("Approbation des comptes 2025")

    # An election: free options by a submit, more rows by another, then "Changer de mode",
    # values carried.
    page.locator("#drafts").get_by_role("link", name="Nouveau vote").click()
    page.locator(".method-card").get_by_role("link", name="Choix unique", exact=True).click()
    page.locator("#editor-title").fill("Conseil d'administration")
    page.get_by_role("button", name="Options libres").click()  # submits, keeps the title
    expect(page.get_by_role("button", name="Options libres")).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(page.locator("#editor-title")).to_have_value("Conseil d'administration")
    rows = page.locator("input[name=option_label]")
    expect(rows).to_have_count(2)
    page.get_by_role("button", name="Ajouter des options").click()  # submits, keeps values
    expect(rows).to_have_count(2 + 5)
    expect(page.locator("#editor-title")).to_have_value("Conseil d'administration")
    for index, name in enumerate(["Alice", "Bruno", "Chloé", "David", "Emma"]):
        rows.nth(index).fill(name)
    page.get_by_role("button", name="Changer de mode").click()
    page.locator("label[for=switch-approval]").click()
    page.get_by_role("button", name="Continuer").click()
    expect(page.locator(".editor-method .method-name")).to_contain_text("Vote par approbation")
    expect(page.locator("#editor-title")).to_have_value("Conseil d'administration")
    expect(rows.nth(4)).to_have_value("Emma")
    page.locator("#editor-seats").fill("2")
    page.get_by_text("Modifier la règle").click()
    page.locator("#min-share-percent").fill("25")
    shot(page, "editor-nojs-phone")
    page.get_by_role("button", name="Enregistrer le brouillon").click()
    expect(page.get_by_text("Vote créé en brouillon.")).to_be_visible()

    with world.database.read() as db:
        listed = polls.list_polls(db, owner, public_id)
        launched = next(poll for poll in listed if poll.title == "Approbation des comptes 2025")
        assert (launched.status, launched.method) == (PollStatus.OPEN, "single_choice")
        assert launched.params == {"threshold": "majority", "adopt_option": 1}
        assert launched.blank_label == "Abstention" and launched.closes_at is not None
        assert [option.label for option in launched.options] == ["Pour", "Contre"]
        election = next(poll for poll in listed if poll.title == "Conseil d'administration")
        assert election.status is PollStatus.DRAFT
        assert election.params == {
            "seats": 2,
            "max_choices": 2,
            "min_share": {"fraction": "1/4", "strict": False},
        }
        assert [option.label for option in election.options] == [
            "Alice",
            "Bruno",
            "Chloé",
            "David",
            "Emma",
        ]
    context.close()


def test_condorcet_keeps_its_rule_sentence_as_options_are_typed(
    phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """editor.js rewrites the sentence on every change, from the texts the page carries: the
    Condorcet rule stays the same whatever the options."""
    public_id = world.assembly(owner, in_session=False)
    admin = organizer(phone, errors, owner, public_id)
    new_poll(admin, "Méthode Condorcet")
    expect(admin.get_by_role("heading", name="Options à classer")).to_be_visible()
    sentence = admin.locator("[data-rule-sentence]")
    rule = (
        "L'option préférée à chacune des autres, en duel, l'emporte\u00a0; "
        "un cycle se règle par les chaînes de duels gagnés les plus fortes (Schulze)."
    )
    expect(sentence).to_have_text(rule)
    admin.locator("#editor-title").fill("Nom de la lettre")
    rows = admin.locator("input[name=option_label]")
    expect(rows).to_have_count(2)
    for row, label in zip(range(3), ("Gazette", "Écho", "Lien"), strict=True):
        if row == rows.count():
            admin.get_by_role("button", name="Ajouter des options").click()
        rows.nth(row).fill(label)
    expect(sentence).to_have_text(rule)
    admin.get_by_role("button", name="Enregistrer le brouillon").click()
    expect(admin.get_by_text("Vote créé en brouillon.")).to_be_visible()
    with world.database.read() as db:
        (poll,) = polls.list_polls(db, owner, public_id)
        assert (poll.method, poll.params) == ("condorcet", {})
        assert [option.label for option in poll.options] == ["Gazette", "Écho", "Lien"]
    assert errors.unexpected() == []


def test_the_hint_of_a_resolution_follows_the_base_chosen(
    phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    public_id = world.assembly(owner, in_session=False)
    admin = organizer(phone, errors, owner, public_id)
    new_poll(admin, "Résolution")
    valid = admin.locator('[data-base-hint="valid"]')
    cast = admin.locator('[data-base-hint="cast"]')
    expect(valid).to_be_visible()
    expect(cast).to_be_hidden()
    admin.locator("#rule-edit > summary").click()
    admin.get_by_label("Bulletins déposés").check()
    expect(cast).to_be_visible()
    expect(cast).to_contain_text("tous les bulletins, votes blancs compris")
    expect(valid).to_be_hidden()
    assert errors.unexpected() == []


def test_the_forms_of_a_single_choice_switch_in_place(
    phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """ "Résolution" and "Options libres" of "Choix unique": the form changes without a reload,
    keeping the title, the rule chosen and the free options typed."""
    public_id = world.assembly(owner, in_session=False)
    admin = organizer(phone, errors, owner, public_id)
    new_poll(admin, "Résolution")
    admin.evaluate("window.notReloaded = true")
    resolution = admin.get_by_role("button", name="Résolution")
    free = admin.get_by_role("button", name="Options libres")
    sentence = admin.locator("[data-rule-sentence]")
    admin.locator("#editor-title").fill("Lieu de l'AG 2027")
    expect(admin.get_by_role("textbox", name="Titre de la résolution")).to_be_visible()
    free.click()
    expect(free).to_have_attribute("aria-pressed", "true")
    expect(resolution).to_have_attribute("aria-pressed", "false")
    expect(admin.locator("#editor-title")).to_have_value("Lieu de l'AG 2027")
    expect(admin.get_by_role("textbox", name="Question", exact=True)).to_be_visible()
    rows = admin.locator("input[name=option_label]")
    expect(rows).to_have_count(2)
    rows.nth(0).fill("Salle des fêtes")
    rows.nth(1).fill("Médiathèque")
    admin.get_by_text("Modifier la règle").click()
    admin.locator("label[for=threshold-two_thirds]").click()
    # To a resolution: its own labels, the rule chosen kept.
    resolution.click()
    expect(resolution).to_have_attribute("aria-pressed", "true")
    expect(resolution).to_be_focused()
    expect(admin.locator(".option-preview")).to_contain_text("Pour")
    expect(sentence).to_have_text(
        "Adoptée si «\u00a0Pour\u00a0» obtient au moins les deux tiers des suffrages exprimés."
    )
    shot(admin, "editor-resolution-phone")
    assert admin.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    # Back to free options: the options typed come back, the editor still works in place.
    free.click()
    expect(rows).to_have_count(4)  # the two options, then two empty rows
    assert rows.evaluate_all("inputs => inputs.map(input => input.value)") == [
        "Salle des fêtes",
        "Médiathèque",
        "",
        "",
    ]
    expect(admin.locator("#threshold-two_thirds")).to_be_checked()
    rows.nth(2).fill("Parc")
    expect(sentence).to_have_text(
        "L'option qui obtient au moins les deux tiers des suffrages exprimés l'emporte."
    )
    admin.get_by_role("button", name="Ajouter des options").click()
    expect(rows).to_have_count(5)
    assert admin.evaluate("window.notReloaded === true")
    admin.get_by_role("button", name="Enregistrer le brouillon").click()
    expect(admin.get_by_text("Vote créé en brouillon.")).to_be_visible()
    expect(admin.locator("#drafts")).to_contain_text("Choix unique · Salle des fêtes")
    with world.database.read() as db:
        (poll,) = polls.list_polls(db, owner, public_id)
        assert (poll.title, poll.method, poll.params) == (
            "Lieu de l'AG 2027",
            "single_choice",
            {"threshold": "two_thirds"},
        )
        assert [o.label for o in poll.options] == ["Salle des fêtes", "Médiathèque", "Parc"]
        assert poll.blank_label is None
    assert errors.unexpected() == []
