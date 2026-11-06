"""Limits on PDF documents (SPEC §9.3): one render at a time in the process, a few per account,
and every code generated in a session counted against its quota (FR-08)."""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agaura import pdf
from agaura.models import AccessMode, User
from agaura.services import access, accounts
from agaura.web.manage.common import PDF_RENDERS_PER_ACCOUNT, pdf_renders_key
from tests.conftest import World
from tests.web.conftest import log_in, make_account, post


@pytest.fixture(autouse=True)
def short_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf, "RENDER_WAIT", 0.1)


def batch_count(world: World, owner: User, public_id: str) -> int:
    with world.database.read() as db:
        return len(access.list_code_batches(db, owner, public_id))


def test_busy_renderer_answers_503_without_waiting_long(
    app: FastAPI, admin: TestClient, world: World, owner: User
) -> None:
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    with pdf.render_slot():  # another document is being rendered
        start = time.monotonic()
        minutes = admin.get(f"/manage/{public_id}/minutes.pdf")
        cards = post(admin, f"/manage/{public_id}/codes", {"count": "3", "output": "pdf"})
        elapsed = time.monotonic() - start
        page = post(admin, f"/manage/{public_id}/codes", {"count": "3", "output": "page"})
    assert elapsed < 2
    for response in (minutes, cards):
        assert response.status_code == 503
        assert response.headers["retry-after"] == str(pdf.RETRY_AFTER)
        assert "Un autre document PDF est en cours de création." in response.text
    assert page.status_code == 200  # the printable page needs no renderer
    with world.database.read() as db:
        assert [batch.size for batch in access.list_code_batches(db, owner, public_id)] == [3]
    # Refused attempts count: retries cannot pile up threads waiting for the renderer.
    assert app.state.limiter.count(pdf_renders_key(owner), PDF_RENDERS_PER_ACCOUNT[1]) == 2
    # Once the renderer is free, both documents come.
    assert admin.get(f"/manage/{public_id}/minutes.pdf").content.startswith(b"%PDF-")
    cards = post(admin, f"/manage/{public_id}/codes", {"count": "3", "output": "pdf"})
    assert cards.content.startswith(b"%PDF-") and batch_count(world, owner, public_id) == 2


def test_pdfs_per_account(app: FastAPI, admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    limit, _window = PDF_RENDERS_PER_ACCOUNT
    for _ in range(limit - 1):
        app.state.limiter.record(pdf_renders_key(owner))
    assert admin.get(f"/manage/{public_id}/minutes.pdf").status_code == 200  # the last one
    refused = admin.get(f"/manage/{public_id}/minutes.pdf")
    assert refused.status_code == 429
    assert "Trop de documents PDF ont été demandés en peu de temps." in refused.text
    cards = post(admin, f"/manage/{public_id}/codes", {"count": "3", "output": "pdf"})
    assert cards.status_code == 429 and batch_count(world, owner, public_id) == 0
    page = post(admin, f"/manage/{public_id}/codes", {"count": "3", "output": "page"})
    assert page.status_code == 200  # not a PDF

    other = make_account(world, "other@example.org")
    other_id = world.assembly(other)
    browser = TestClient(app, follow_redirects=False)
    log_in(browser, other.email)
    assert browser.get(f"/manage/{other_id}/minutes.pdf").status_code == 200


def test_revoked_codes_count_toward_the_quota(admin: TestClient, world: World, owner: User) -> None:
    with world.database.write() as db:
        accounts.set_quotas(db, owner.email, {"codes_per_assembly": 5})
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    assert post(admin, f"/manage/{public_id}/codes", {"count": "3"}).status_code == 200
    with world.database.read() as db:
        batch = access.list_code_batches(db, owner, public_id)[0]
    post(admin, f"/manage/{public_id}/codes/{batch.id}/revoke")
    refused = post(admin, f"/manage/{public_id}/codes", {"count": "3", "output": "pdf"})
    assert refused.status_code == 303
    console = admin.get(f"/manage/{public_id}").text
    assert "Cette session a atteint le nombre maximal de codes, codes révoqués compris." in console
    assert batch_count(world, owner, public_id) == 1
