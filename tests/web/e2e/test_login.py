"""Logging in while someone else keeps failing on the account: the anti-robot check in the page
(SPEC §9.3)."""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from playwright.sync_api import BrowserContext, expect

from agaura.config import SignupMode
from agaura.models import User
from agaura.security import hash_token
from agaura.web import public
from tests.web.conftest import PASSWORD
from tests.web.e2e.conftest import ErrorWatch

pytestmark = pytest.mark.e2e


def test_owner_logs_in_through_the_anti_robot_check(
    app: FastAPI, desktop: BrowserContext, errors: ErrorWatch, owner: User
) -> None:
    for _ in range(public.LOGIN_FAILURES_BEFORE_POW[0]):  # failures from other addresses
        app.state.limiter.record(f"login-email:{hash_token(owner.email)}")
    page = errors.watch(desktop.new_page())
    page.goto("/login")
    page.fill("#field-email", owner.email)
    page.fill("#field-password", PASSWORD)
    page.get_by_role("button", name="Se connecter").click()
    expect(page.get_by_text("une vérification anti-robot est nécessaire")).to_be_visible()
    expect(page.locator("#field-email")).to_have_value(owner.email)
    page.fill("#field-password", PASSWORD)
    submit = page.get_by_role("button", name="Se connecter")
    expect(submit).to_be_enabled(timeout=15_000)  # the proof of work is solved in the page
    submit.click()
    expect(page).to_have_url(re.compile(r"/dashboard$"))
    expect(page.get_by_text("Vous êtes connecté·e.")).to_be_visible()
    # The 400 answer of the first try shows up as a failed resource in the console.
    assert errors.unexpected("status of 400") == []


def test_links_under_the_forms_are_large_enough_to_tap(
    app: FastAPI, desktop: BrowserContext, phone: BrowserContext, errors: ErrorWatch
) -> None:
    """The link to the forgotten password and the other links under the account forms: boxes
    at least 24 px tall (WCAG 2.5.8, target-size), on a line barely taller than the text."""
    app.state.settings = app.state.settings.model_copy(update={"signup": SignupMode.OPEN})
    expected = {
        "/login": ["Mot de passe oublié\u00a0?", "Créer un compte"],
        "/reset": ["Retour à la page de connexion"],
        "/signup": ["Se connecter"],
    }
    for context in (desktop, phone):
        page = errors.watch(context.new_page())
        for path, names in expected.items():
            page.goto(path)
            links = page.locator(".form-links a")
            expect(links).to_have_text(names)
            for link in links.all():
                box = link.bounding_box()
                assert box is not None and 24 <= box["height"] <= 32, (path, box)
            line = page.locator(".form-links").bounding_box()
            assert line is not None and line["height"] <= 32, (path, line)
        page.close()
    assert errors.unexpected() == []
