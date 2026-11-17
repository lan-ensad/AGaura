"""An aura battle in a real browser (SPEC §6.8): two phones choose and tap, the projection
screen follows the gauges and totals live, freezes them at the end, then shows the ranking by
itself. Takes about 20 s: the 10 s of the choice and the 5 s of the taps run in real time."""

from __future__ import annotations

from fastapi import FastAPI
from playwright.sync_api import Browser, Page, Playwright, expect

from agaura.models import User
from agaura.services.polls import PollInput
from agaura.voting.aura import CHOOSE_SECONDS
from agaura.web import aura
from tests.conftest import World
from tests.web.e2e.conftest import ErrorWatch, new_phone
from tests.web.screen.conftest import screen_url

BATTLE = PollInput("Thème de la fête", "aura", ["Pirates", "Disco", "Western"], {"duration": 5})
WAIT = (CHOOSE_SECONDS + 3) * 1000


def joined(
    browser: Browser, pw: Playwright, live_server: str, url: str, errors: ErrorWatch
) -> Page:
    page = errors.watch(new_phone(browser, pw, live_server).new_page())
    page.goto(url)
    expect(page.locator("#live")).to_be_visible()
    return page


def test_a_battle_from_the_phones_to_the_screen(
    app: FastAPI,
    browser: Browser,
    pw: Playwright,
    live_server: str,
    world: World,
    owner: User,
    errors: ErrorWatch,
) -> None:
    public_id = world.assembly(owner, title="AG de l'association")
    join_url = f"/{public_id}#k={world.password(owner, public_id)}"
    fast = joined(browser, pw, live_server, join_url, errors)
    slow = joined(browser, pw, live_server, join_url, errors)
    late = joined(browser, pw, live_server, join_url, errors)
    projector = browser.new_context(base_url=live_server, viewport={"width": 1920, "height": 1080})
    screen = errors.watch(projector.new_page())
    screen.goto(screen_url(world, owner, public_id))

    world.poll(owner, public_id, BATTLE)  # pushed, as "Lancer" does from the console
    aura.sync_battles(app.state.database, app.state.arena, public_id)
    app.state.hub.publish(f"live:{public_id}", "update", "polls")
    app.state.hub.publish(f"manage:{public_id}", "turnout", "console")

    stage = screen.locator("[data-battle]")
    expect(stage).to_be_visible()
    for phone in (fast, slow, late):
        expect(phone.locator("[data-aura] [data-aura-countdown]")).not_to_be_empty()
    fast.get_by_text("Pirates", exact=True).click()
    fast.get_by_text("Disco", exact=True).click()  # changed its mind while choosing
    slow.get_by_text("Western", exact=True).click()

    button = fast.locator("[data-aura-button]")
    expect(button).to_be_visible(timeout=WAIT)
    expect(button).to_contain_text("Disco")
    expect(slow.locator("[data-aura-button]")).to_contain_text("Western")
    expect(late.locator("[data-aura-button]")).to_be_hidden()
    expect(late.locator("[data-aura-status]")).to_contain_text("Aucune option")
    expect(fast.locator(".aura-options .choice:not([hidden])")).to_have_count(1)

    # force: each tap restarts a small animation of the button, which Playwright would wait on.
    for _ in range(25):
        button.click(force=True)
    for _ in range(3):
        slow.locator("[data-aura-button]").click(force=True)
    expect(fast.locator("[data-aura-count]")).to_have_text("25")

    totals = stage.locator("[data-total]")
    expect(totals.nth(1)).not_to_have_text("0", timeout=3000)  # Disco, live
    expect(screen.locator("[data-phones]")).to_contain_text("2")

    # Over: the phone says so, then the ranking takes the stage by itself, Disco first.
    expect(fast.locator("[data-aura-status]")).to_contain_text("Terminé", timeout=8000)
    result = screen.locator(".screen-result.is-battle")
    expect(result).to_be_visible(timeout=6000)
    expect(result.locator(".screen-verdict")).to_contain_text("Disco")
    expect(result.locator("li.is-win")).to_contain_text("Disco")
    taps = int(result.locator("li.is-win [data-taps]").get_attribute("data-taps") or 0)
    assert 3 < taps <= 25  # the allowance of a phone: 15 taps a second at most
    expect(fast.locator("[data-aura]")).to_have_count(0)  # closed: gone from the phones

    assert errors.unexpected() == []
    projector.close()
