"""The ballot box in a browser (SPEC §6.6, FR-42, FR-43): counters in what remains, boxes
blocked at the maximum, the lists of ranks of a Condorcet ballot, the summary of the open polls
that names a poll waiting for its confirmation, "Vote suivant" after a vote, and the banner of a
lost connection."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import BrowserContext, Page, expect

from agaura.models import User
from agaura.services.polls import PollInput
from tests.conftest import World
from tests.web.e2e.conftest import ErrorWatch
from tests.web.live.conftest import CONDORCET, MAJORITY_JUDGMENT, ballots_of

pytestmark = pytest.mark.e2e

BOARD = PollInput(
    "Conseil d'administration",
    "approval",
    ["Alice Martin", "Bruno Petit", "Chloé Durand", "David Leroy"],
    params={"seats": 2, "max_choices": 2},
)


def join_page(phone: BrowserContext, errors: ErrorWatch, world: World, owner: User, public_id: str):
    voter = errors.watch(phone.new_page())
    voter.goto(f"/{public_id}#k={world.password(owner, public_id)}")
    expect(voter.locator("#live")).to_be_attached()
    return voter


def box_of(page: Page, selector: str) -> dict[str, float]:
    box = page.locator(selector).bounding_box()
    assert box is not None, selector
    return box


def test_approval_counts_what_remains_and_blocks_the_boxes_at_the_maximum(
    phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, BOARD)
    voter = join_page(phone, errors, world, owner, public_id)
    ballot = voter.locator(f"#poll-{poll_id}")
    counter = ballot.get_by_role("status")
    expect(counter).to_have_text("Encore 2 choix possibles.")
    # The blank vote right after the last option, one more row like it; then the counter.
    last = box_of(voter, f"#poll-{poll_id} label:has-text('David Leroy')")
    blank = box_of(voter, f"#poll-{poll_id} label:has([data-blank])")
    shown = box_of(voter, f"#poll-{poll_id}-counter")
    assert last["y"] + last["height"] < blank["y"] < last["y"] + last["height"] + 16
    assert (blank["x"], blank["width"], blank["height"]) == (
        last["x"],
        last["width"],
        last["height"],
    )
    assert blank["y"] + blank["height"] <= shown["y"]

    ballot.get_by_text("Alice Martin", exact=True).click()
    expect(counter).to_have_text("Encore 1 choix possible.")
    ballot.get_by_text("Chloé Durand", exact=True).click()
    expect(counter).to_have_text(
        "Maximum atteint\u00a0: 2 choix sur 2.Décochez une option pour en choisir une autre."
    )
    expect(counter).to_have_class(re.compile(r"\bis-full\b"))
    for name in ("Bruno Petit", "David Leroy"):
        box = ballot.get_by_label(name)
        expect(box).to_have_attribute("aria-disabled", "true")
        expect(box).not_to_have_attribute("disabled", "")
        expect(box).to_have_accessible_description(
            re.compile("^Maximum atteint\u00a0: 2 choix sur 2")
        )
    # Still focusable; neither a tap nor the space bar ticks it.
    blocked = ballot.get_by_label("Bruno Petit")
    blocked.focus()
    expect(blocked).to_be_focused()
    voter.keyboard.press("Space")
    expect(blocked).not_to_be_checked()
    ballot.get_by_text("Bruno Petit", exact=True).click(force=True)  # Playwright waits otherwise
    expect(blocked).not_to_be_checked()
    expect(counter).to_contain_text("Maximum atteint")  # read out again, the same words

    ballot.get_by_text("Alice Martin", exact=True).click()  # one fewer: every box free again
    expect(counter).to_have_text("Encore 1 choix possible.")
    expect(ballot.locator("[aria-disabled]")).to_have_count(0)
    ballot.get_by_text("Bruno Petit", exact=True).click()
    expect(ballot.get_by_label("Bruno Petit")).to_be_checked()
    ballot.get_by_role("button", name="Continuer").click()
    expect(ballot.locator(".pending-flag")).to_have_text("Bulletin pas encore déposé")
    assert errors.unexpected() == []


def test_condorcet_lists_of_ranks_the_counter_and_the_blank_box(
    phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, CONDORCET)
    voter = join_page(phone, errors, world, owner, public_id)
    ballot = voter.locator(f"#poll-{poll_id}")
    counter = ballot.locator("[data-ranked]")
    expect(counter).to_have_text("0 option sur 4 classée")
    # The blank vote, one more row of the ranking: its name where the options' names are, its
    # box in the column of the lists, the whole row a target of 48 px.
    rows = ballot.locator(".rank-option")
    expect(rows).to_have_count(len(CONDORCET.options) + 1)
    fil, row = rows.nth(len(CONDORCET.options) - 1).bounding_box(), rows.last.bounding_box()
    assert fil is not None and row is not None
    assert (row["x"], row["width"]) == (fil["x"], fil["width"]) and row["y"] > fil["y"]
    assert row["height"] >= 48
    names = ballot.locator(".rank-name")
    assert names.last.bounding_box()["x"] == names.first.bounding_box()["x"]  # type: ignore[index]
    blank = ballot.get_by_label("Vote blanc")
    lists = ballot.locator("select").last.bounding_box()
    box = blank.bounding_box()
    assert lists is not None and box is not None and lists["x"] <= box["x"] < lists["x"] + 32
    blank.check()
    ballot.get_by_label("Écho").select_option("1er choix")
    expect(blank).not_to_be_checked()  # a rank chosen: no blank vote any more
    ballot.get_by_label("Gazette").select_option("2e choix")
    ballot.get_by_label("Lien").select_option("2e choix")
    expect(counter).to_have_text("3 options sur 4 classées")
    blank.check()  # the blank vote empties every list
    expect(counter).to_have_text("0 option sur 4 classée")
    for name in CONDORCET.options:
        expect(ballot.get_by_label(name, exact=True)).to_have_value("00")  # "Non classée"
    blank.uncheck()
    for name, rank in (("Écho", "1er choix"), ("Gazette", "2e choix"), ("Lien", "2e choix")):
        ballot.get_by_label(name, exact=True).select_option(rank)
    expect(counter).to_have_class(re.compile(r"^counter$"))
    ballot.get_by_label("Fil").select_option("4e choix")
    expect(counter).to_have_class(re.compile(r"\bis-complete\b"))
    ballot.get_by_label("Fil").select_option("Non classée")
    ballot.get_by_role("button", name="Continuer").click()

    ranks = ballot.locator(".summary-ranks li")
    expect(ranks).to_have_text(
        ["1er choix Écho", "2e choix Gazette", "2e choix Lien", "Non classée Fil"]
    )
    default = ballot.locator(".summary-ranks .rank.is-default")
    assert default.evaluate("el => getComputedStyle(el).borderTopStyle") == "dashed"
    ballot.get_by_role("button", name="Confirmer mon vote").click()
    expect(ballot.get_by_role("heading", name="Vote enregistré")).to_be_focused()
    assert ballots_of(world, poll_id) == [(False, {"ranks": {"1": 2, "2": 1, "3": 2, "4": 3}})]
    assert voter.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert errors.unexpected() == []


def test_the_blank_vote_with_the_keyboard_alone(
    phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """SPEC §6.6, NFR-06: the blank vote comes next in the tab order after the last option, its
    focus drawn, and the space bar chooses it; its summary names it with the ballot's words."""
    public_id = world.assembly(owner)
    judgment = world.poll(owner, public_id, MAJORITY_JUDGMENT)
    ranking = world.poll(owner, public_id, CONDORCET)
    voter = join_page(phone, errors, world, owner, public_id)

    grid = voter.locator(f"#poll-{judgment}")
    grid.locator("fieldset", has_text="Banc").get_by_text("Bien", exact=True).click()
    grid.locator("input[name=grade-03]:checked").focus()
    voter.keyboard.press("Tab")
    blank = grid.get_by_label("Vote blanc")
    expect(blank).to_be_focused()
    pill = grid.locator(".mj-blank .grade-pill")
    assert pill.evaluate("el => getComputedStyle(el).outlineStyle") == "solid"
    voter.keyboard.press("Space")
    expect(blank).to_be_checked()
    expect(grid.locator("input[name^=grade-]:checked")).to_have_count(0)
    voter.keyboard.press("Tab")
    expect(grid.get_by_role("button", name="Continuer")).to_be_focused()
    voter.keyboard.press("Enter")
    expect(grid.locator(".summary-main")).to_have_text("Vote blanc")

    ballot = voter.locator(f"#poll-{ranking}")
    ballot.get_by_label("Fil").select_option("1er choix")
    ballot.get_by_label("Fil").focus()
    voter.keyboard.press("Tab")
    blank = ballot.get_by_label("Vote blanc")
    expect(blank).to_be_focused()
    voter.keyboard.press("Space")
    expect(blank).to_be_checked()
    expect(ballot.get_by_label("Fil")).to_have_value("00")  # "Non classée"
    voter.keyboard.press("Space")
    expect(blank).not_to_be_checked()
    assert errors.unexpected() == []


def test_a_poll_in_review_and_the_next_vote(
    phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """The summary says which poll waits for its confirmation on this device; after the vote,
    "Vote suivant" leads to the next poll still to vote, on the same page."""
    public_id = world.assembly(owner)
    options = ["Jardin partagé", "Fresque murale", "Atelier vélo"]
    third = world.poll(owner, public_id, PollInput("Lieu", "single_choice", ["Salle", "Parc"]))
    second = world.poll(owner, public_id, PollInput("Projets", "majority_judgment", options))
    first = world.poll(owner, public_id, PollInput("Budget", "single_choice", ["Oui", "Non"]))
    voter = join_page(phone, errors, world, owner, public_id)
    summary = voter.get_by_role("navigation", name="Votes ouverts")
    expect(summary.get_by_role("link")).to_have_text(["Budget", "Projets", "Lieu"])

    grid = voter.locator(f"#poll-{second}")
    grid.locator("fieldset", has_text="Jardin partagé").get_by_text("Bien", exact=True).click()
    grid.get_by_role("button", name="Continuer").click()
    expect(grid.locator(".pending-flag")).to_have_text("Bulletin pas encore déposé")
    expect(grid.locator(".stepper [aria-current='step']")).to_have_text("Vérifier")
    link = summary.get_by_role("link", name="À confirmer · Projets")
    expect(link).to_have_class(re.compile(r"\bis-review\b"))
    expect(link).to_have_attribute("href", f"#poll-{second}")
    defaults = grid.locator(".summary-grades .grade.is-default")
    expect(defaults).to_have_count(2)
    style = defaults.first.evaluate("el => getComputedStyle(el).borderTopStyle")
    assert style == "dashed"

    grid.get_by_role("button", name="Modifier mon vote").click()
    expect(summary.get_by_role("link")).to_have_text(["Budget", "Projets", "Lieu"])
    grid.get_by_role("button", name="Continuer").click()
    grid.get_by_role("button", name="Confirmer mon vote").click()
    expect(grid.get_by_role("heading", name="Vote enregistré")).to_be_focused()
    expect(grid.get_by_text("enregistré")).to_have_count(1)  # said once
    expect(grid.locator(".stepper .is-done")).to_have_count(3)
    following = grid.get_by_role("link", name="Vote suivant\u00a0: Lieu")  # the next one below
    expect(following).to_have_attribute("href", f"#poll-{third}")
    expect(summary.get_by_role("link")).to_have_text(["Budget", "Lieu"])
    following.click()
    expect(voter).to_have_url(re.compile(f"#poll-{third}$"))
    heading, bar = box_of(voter, f"#poll-{third}-title"), box_of(voter, "nav.live-summary")
    assert heading["y"] >= bar["y"] + bar["height"] - 1  # not under the summary

    # The last one below voted: the next vote is above, with the arrow turned up.
    lieu = voter.locator(f"#poll-{third}")
    lieu.get_by_text("Parc", exact=True).click()
    lieu.get_by_role("button", name="Continuer").click()
    lieu.get_by_role("button", name="Confirmer mon vote").click()
    above = lieu.get_by_role("link", name="Vote suivant\u00a0: Budget")
    expect(above).to_have_attribute("href", f"#poll-{first}")
    expect(lieu.locator(".next-vote")).to_have_class(re.compile(r"\bis-above\b"))
    assert voter.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert errors.unexpected() == []


def test_a_lost_connection_shows_a_banner_that_moves_nothing(
    phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """FR-42: after a few seconds without the stream nor an answer, a status fixed at the
    bottom; the ballot being filled in stays as it is; the banner goes once the network is
    back."""
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, BOARD)
    voter = join_page(phone, errors, world, owner, public_id)
    ballot = voter.locator(f"#poll-{poll_id}")
    ballot.get_by_text("Chloé Durand", exact=True).click()
    banner = voter.locator("[data-net-banner]")
    expect(banner).to_have_attribute("role", "status")
    expect(banner).to_have_text("")
    before = box_of(voter, f"#poll-{poll_id}")

    phone.set_offline(True)
    expect(banner).to_have_text(
        "Connexion perdue, reconnexion en cours… Le bulletin entamé reste en place.",
        timeout=15_000,
    )
    place = box_of(voter, "[data-net-banner]")
    viewport = voter.viewport_size
    assert viewport is not None and abs(place["y"] + place["height"] - viewport["height"]) < 1
    assert box_of(voter, f"#poll-{poll_id}") == before  # nothing moved
    expect(ballot.get_by_label("Chloé Durand")).to_be_checked()
    assert voter.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    phone.set_offline(False)
    expect(banner).to_have_text("Connexion rétablie", timeout=15_000)
    expect(banner).to_have_text("", timeout=10_000)
    assert box_of(voter, f"#poll-{poll_id}") == before
    expect(ballot.get_by_label("Chloé Durand")).to_be_checked()
    ballot.get_by_role("button", name="Continuer").click()  # the network works again
    expect(ballot.locator(".pending-flag")).to_be_visible()
    assert errors.unexpected("ERR_INTERNET_DISCONNECTED", "Failed to load resource") == []


def test_a_refused_stream_alone_shows_no_banner(
    phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    """The banner tells a lost network, not a stream refused while the server still answers:
    the page then polls (FR-42)."""
    public_id = world.assembly(owner)
    world.poll(owner, public_id, BOARD)
    voter = errors.watch(phone.new_page())
    voter.route("**/events", lambda route: route.abort())
    voter.goto(f"/{public_id}#k={world.password(owner, public_id)}")
    expect(voter.get_by_role("heading", name="Conseil d'administration")).to_be_visible()
    voter.wait_for_timeout(7_000)  # past the 5 s of grace
    expect(voter.locator("[data-net-banner]")).to_have_text("")
    assert errors.unexpected("ERR_FAILED", "Failed to load resource") == []
