"""Super-admin journey on a phone: invite an account, the invited person chooses a password from
the emailed link and lands on an empty dashboard, then the super-admin deletes the account.
Set ``AGAURA_SHOTS_DIR`` to keep screenshots."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, expect
from sqlalchemy import select

from agaura.models import OutboxEmail, User
from agaura.services import accounts
from tests.conftest import World
from tests.web.conftest import PASSWORD
from tests.web.e2e.conftest import ErrorWatch, new_phone

pytestmark = pytest.mark.e2e

SHOTS = os.environ.get("AGAURA_SHOTS_DIR")
ROOT = "root@example.org"
INVITED = "camille@example.org"
NEW_PASSWORD = "my own long passphrase"


def shot(page: Page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=Path(SHOTS) / f"superadmin-{name}.png", full_page=True)


def log_in(page: Page, email: str, password: str = PASSWORD) -> None:
    page.goto("/login")
    page.fill("#field-email", email)
    page.fill("#field-password", password)
    page.locator("form[action='/login'] button[type=submit]").click()
    expect(page.get_by_text("Vous êtes connecté·e.")).to_be_visible()


def invitation_path(world: World) -> str:
    with world.database.read() as db:
        body = db.scalar(
            select(OutboxEmail.body_text)
            .where(OutboxEmail.to_address == INVITED)
            .order_by(OutboxEmail.id.desc())
        )
    assert body is not None
    match = re.search(r"https?://[^/\s]+(/invite/[A-Za-z0-9_-]+)", body)
    assert match, body
    return match.group(1)


def test_invite_then_delete_an_account(
    browser: Browser,
    pw: Playwright,
    live_server: str,
    phone: BrowserContext,
    errors: ErrorWatch,
    world: World,
) -> None:
    with world.database.write() as db:
        accounts.create_user_by_operator(db, ROOT, PASSWORD, superadmin=True)

    # The home page and the login page no longer offer signup.
    root = errors.watch(phone.new_page())
    root.goto("/")
    expect(
        root.get_by_text("Pour obtenir un compte, écrivez à contact@agaura.org.")
    ).to_be_visible()
    assert root.locator("a[href='mailto:contact@agaura.org']").count() == 1
    assert root.locator("a[href='/signup']").count() == 0
    shot(root, "home-closed")
    root.goto("/signup")
    heading = root.get_by_role("heading", name="Les comptes sont créés sur invitation")
    expect(heading).to_be_visible()
    shot(root, "signup-closed")

    # The super-admin lands on the accounts page, and never reaches the dashboard.
    log_in(root, ROOT)
    expect(root).to_have_url(re.compile(r"/superadmin$"))
    root.goto("/dashboard")
    expect(root.get_by_role("heading", name="Non disponible pour le super-admin")).to_be_visible()
    shot(root, "refused")
    root.get_by_role("link", name="Aller aux comptes").click()

    root.fill("#field-email", INVITED)
    root.fill("#field-display_name", "Camille Martin")
    root.get_by_role("button", name="Créer le compte et envoyer l'invitation").click()
    expect(root.get_by_text("Compte créé. L'invitation est envoyée")).to_be_visible()
    item = root.locator("li.sa-account").filter(has_text=INVITED)
    expect(item.get_by_text("Invitation en attente")).to_be_visible()
    shot(root, "accounts")

    # The invited person opens the link on another phone and chooses a password.
    invited_context = new_phone(browser, pw, live_server)
    try:
        invited = errors.watch(invited_context.new_page())
        invited.goto(invitation_path(world))
        expect(invited.get_by_role("heading", name="Bienvenue sur AGaura")).to_be_visible()
        expect(invited.locator("#field-email")).to_have_value(INVITED)
        shot(invited, "invitation")
        invited.fill("#field-password", NEW_PASSWORD)
        invited.fill("#field-password_confirm", NEW_PASSWORD)
        invited.get_by_role("button", name="Enregistrer le mot de passe et se connecter").click()
        expect(invited).to_have_url(re.compile(r"/dashboard$"))
        expect(invited.get_by_text("Votre mot de passe est enregistré")).to_be_visible()
        expect(invited.get_by_text("Aucune session pour l'instant.")).to_be_visible()
        shot(invited, "dashboard")
    finally:
        invited_context.close()

    # Back on the accounts page: the account is active; the super-admin deletes it.
    root.reload()
    item = root.locator("li.sa-account").filter(has_text=INVITED)
    expect(item.get_by_text("Actif")).to_be_visible()
    item.get_by_text("Supprimer ce compte").click()
    item.locator("input[name=confirm_email]").fill(INVITED)
    # The super-admin's own password: a wrong one deletes nothing.
    item.get_by_label("Votre mot de passe").fill("not the password")
    shot(root, "delete")
    item.get_by_role("button", name="Supprimer le compte").click()
    expect(root.get_by_text("Mot de passe incorrect.")).to_be_visible()
    item = root.locator("li.sa-account").filter(has_text=INVITED)
    expect(item).to_have_count(1)
    item.get_by_text("Supprimer ce compte").click()
    item.locator("input[name=confirm_email]").fill(INVITED)
    item.get_by_label("Votre mot de passe").fill(PASSWORD)
    item.get_by_role("button", name="Supprimer le compte").click()
    expect(root.get_by_text("Compte supprimé, avec toutes ses sessions.")).to_be_visible()
    expect(root.locator("li.sa-account").filter(has_text=INVITED)).to_have_count(0)
    with world.database.read() as db:
        assert db.scalar(select(User).where(User.email == INVITED)) is None

    root.goto("/account")
    expect(root.get_by_role("heading", name="Compte super-admin")).to_be_visible()
    assert root.locator("form[action='/account/delete']").count() == 0
    shot(root, "account")
    # The only console error: Chromium reports the 403 status of the refused dashboard.
    assert errors.unexpected("status of 403") == []
    assert len(errors.errors) == 1
