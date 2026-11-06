"""End-to-end tests in a real browser (PLAN P4): the application runs on a local port and
Chromium drives it, phones in a mobile viewport. Skipped when Playwright's Chromium is not
installed (``uv run playwright install chromium``)."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from fastapi import FastAPI

playwright_api = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import (  # noqa: E402
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    expect,
)

PHONE = "Pixel 7"


# Module scope: Playwright's synchronous API keeps an event loop running in this thread, which
# would break later tests that start their own loop with ``asyncio.run``.
@pytest.fixture(scope="module")
def pw() -> Iterator[Playwright]:
    with playwright_api.sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="module")
def browser(pw: Playwright) -> Iterator[Browser]:
    try:
        browser = pw.chromium.launch()
    except Exception as error:  # browser binaries missing
        pytest.skip(f"Chromium is not available: {error}")
    yield browser
    browser.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_server(app: FastAPI) -> Iterator[str]:
    """The application served by uvicorn in a background thread."""
    config = uvicorn.Config(
        app, host="127.0.0.1", port=_free_port(), log_level="warning", timeout_graceful_shutdown=1
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            pytest.fail("the test server did not start")
        time.sleep(0.02)
    yield f"http://127.0.0.1:{config.port}"
    server.should_exit = True
    thread.join(10)


class ErrorWatch:
    """JavaScript errors and console errors (CSP violations show up there)."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def watch(self, page: Page) -> Page:
        page.on("pageerror", lambda error: self.errors.append(f"pageerror: {error}"))
        page.on(
            "console",
            lambda message: message.type == "error" and self.errors.append(message.text),
        )
        return page

    def unexpected(self, *allowed: str) -> list[str]:
        return [error for error in self.errors if not any(text in error for text in allowed)]


@pytest.fixture
def errors() -> ErrorWatch:
    return ErrorWatch()


@pytest.fixture
def desktop(browser: Browser, live_server: str) -> Iterator[BrowserContext]:
    context = browser.new_context(base_url=live_server, viewport={"width": 1280, "height": 900})
    yield context
    context.close()


def new_phone(
    browser: Browser, pw: Playwright, live_server: str, **options: object
) -> BrowserContext:
    return browser.new_context(**pw.devices[PHONE], base_url=live_server, **options)  # type: ignore[arg-type]


@pytest.fixture
def phone(browser: Browser, pw: Playwright, live_server: str) -> Iterator[BrowserContext]:
    context = new_phone(browser, pw, live_server)
    yield context
    context.close()


def show_rubric(page: Page, rubric: str) -> None:
    """Show a rubric of the session console (cockpit, polls, meeting, agenda, notes, access,
    screen, documents, settings) through its link, as a person would: the sidebar on a
    computer, the tabs at the bottom of a phone or their "More" menu."""
    href = f"#tab-{rubric}"
    side = page.locator(f".rubric-nav a[href='{href}']")
    tab = page.locator(f".tab-bar > a[href='{href}']")
    if side.is_visible():
        side.click()
    elif tab.count():
        tab.click()
    else:
        page.locator(".tab-more > summary").click()
        page.locator(f".tab-more a[href='{href}']").click()
    expect(page.locator(href)).to_be_visible()


def unfold(tiles: Locator) -> None:
    """Unfold the tiles of polls (Polls rubric, folded by default) that ``tiles`` matches, by
    their summary, as a person would; one already unfolded stays so."""
    for tile in tiles.all():
        if tile.get_attribute("open") is None:
            tile.locator("> summary").click()
            expect(tile).to_have_attribute("open", "")
