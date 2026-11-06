"""Results and exports in a browser (PLAN P6): the public results page on a phone, the finished
polls and the downloads in the console. Set ``AGAURA_SHOTS_DIR`` to keep screenshots."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import BrowserContext, Page, expect

from agaura.exports import recount
from agaura.models import User
from agaura.services import access
from tests.conftest import World
from tests.exports.conftest import build_session
from tests.web.e2e.conftest import ErrorWatch, show_rubric, unfold
from tests.web.e2e.test_journeys import log_in

pytestmark = pytest.mark.e2e

SHOTS = os.environ.get("AGAURA_SHOTS_DIR")


def shot(page: Page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=Path(SHOTS) / f"{name}.png", full_page=True)


def test_public_results_on_a_phone(
    phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    built = build_session(world, owner, close=False)
    with world.database.write() as db:
        token = access.set_public_results(db, world.keyring, owner, built.public_id, True)
    page = errors.watch(phone.new_page())
    page.goto(f"/{built.public_id}/results/{token}")
    expect(page.get_by_role("heading", name="Assemblée générale 2026", level=1)).to_be_visible()
    expect(page.get_by_role("heading", level=2)).to_have_count(5)
    expect(page.get_by_text("Adoptée")).to_be_visible()
    expect(page.get_by_text("Annulé\u00a0: Vote à refaire")).to_be_visible()
    mj = page.locator("section", has_text="Projets de quartier")
    bars = mj.locator("svg.merit")
    expect(bars).to_have_count(2)
    box = bars.first.bounding_box()
    assert box is not None and box["width"] > 150 and 10 < box["height"] < 30
    mj.get_by_text("Voix par mention").click()
    expect(mj.locator("ul.merit-list")).to_be_visible()
    # Nothing wider than the phone screen.
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, "public-results-phone")

    # The same results on a voter's live page, without the results stylesheet.
    voter = errors.watch(phone.new_page())
    voter.goto(f"/{built.public_id}#k={world.password(owner, built.public_id)}")
    card = voter.locator("section.result-card", has_text="Projets de quartier")
    expect(card.locator("svg.merit")).to_have_count(2)
    card.get_by_text("Voix par mention").click()
    expect(card.locator("ul.merit-list")).to_be_visible()
    assert voter.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(voter, "live-results-phone")
    assert errors.unexpected() == []


def test_console_results_and_downloads(
    desktop: BrowserContext, errors: ErrorWatch, world: World, owner: User, tmp_path: Path
) -> None:
    built = build_session(world, owner)
    page = errors.watch(desktop.new_page())
    log_in(page, owner.email)
    page.goto(f"/manage/{built.public_id}#results")
    results = page.locator("#results")
    unfold(results.locator("details.poll-tile"))  # each poll a tile, folded by default
    expect(
        results.get_by_text("Égalité entre Bruno, Chloé\u00a0: appliquez vos statuts")
    ).to_be_visible()
    expect(results.locator("svg.merit")).to_have_count(2)
    expect(results.get_by_text("100,0 % des présents").first).to_be_visible()

    show_rubric(page, "documents")
    with page.expect_download() as info:
        page.locator("#exports").get_by_role("link", name="Données complètes (JSON)").click()
    download = info.value
    assert download.suggested_filename == f"agaura-{built.public_id}-session.json"
    path = tmp_path / download.suggested_filename
    download.save_as(path)
    assert recount.main([str(path)]) == 0
    assert len(json.loads(path.read_text())["polls"]) == 6

    with page.expect_download() as info:
        page.locator("#exports").get_by_role("link", name="Procès-verbal (PDF)").click()
    assert info.value.suggested_filename == f"agaura-{built.public_id}-proces-verbal.pdf"

    # Each poll's exports sit in Documents too (D-H): its finished poll in Votes leads there.
    show_rubric(page, "polls")
    article = results.locator("details.poll-tile", has_text="Élection du conseil")
    unfold(article)  # the page was drawn again: folded
    article.get_by_role("link", name="Exports de ce vote, dans Documents").click()
    row = page.locator(f"#exports-{built.polls['approval']}")
    expect(row).to_be_in_viewport()
    expect(page.locator("#tab-documents")).to_be_visible()
    expect(page.locator("#tab-polls")).to_be_hidden()
    with page.expect_download() as info:
        row.get_by_role("link", name="Bulletins (CSV)").click()
    assert info.value.suggested_filename == f"agaura-{built.public_id}-vote-2-bulletins.csv"
    shot(page, "console-documents-desktop")
    assert errors.unexpected() == []
