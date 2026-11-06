"""Card PDF of individual codes (FR-23): 10 cards per A4 page, codes shown once, never kept."""

from __future__ import annotations

import logging
import re
import zlib
from pathlib import Path

import pytest
import weasyprint
from fastapi.testclient import TestClient

from agaura.models import AccessMode, User
from agaura.pdf import OfflineFetcher
from agaura.security import format_voter_code, new_voter_code, normalize_secret
from agaura.services import access
from agaura.web.manage.common import qr_svg
from agaura.web.templating import templates
from tests.conftest import World
from tests.web.conftest import post

CODE = re.compile(r"[0-9A-Z]{4}-[0-9A-Z]{4}")


def pdf_pages(pdf: bytes) -> int:
    """Pages of a PDF, its object streams decompressed (WeasyPrint compresses them)."""
    chunks = [pdf]
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        try:
            chunks.append(zlib.decompress(match.group(1)))
        except zlib.error:
            continue
    return sum(len(re.findall(rb"/Type\s*/Page(?![A-Za-z])", chunk)) for chunk in chunks)


def cards_html(count: int) -> tuple[str, list[str]]:
    codes = [format_voter_code(new_voter_code()) for _ in range(count)]
    cards = [{"code": code, "qr": qr_svg(f"https://agaura.org/K7M4X2#k={code}")} for code in codes]
    html = templates.env.get_template("manage/cards_pdf.html").render(
        title="Assemblée générale",
        voter_url="https://agaura.org/K7M4X2",
        display_url="agaura.org/K7M4X2",
        public_id="K7M4X2",
        cards=cards,
        pages=[cards[start : start + 10] for start in range(0, count, 10)],
    )
    return html, codes


@pytest.mark.parametrize(("count", "pages"), [(1, 1), (10, 1), (11, 2), (25, 3)])
def test_ten_cards_per_page(count: int, pages: int) -> None:
    html, codes = cards_html(count)
    document = weasyprint.HTML(string=html, url_fetcher=OfflineFetcher()).render()
    assert len(document.pages) == pages
    assert all(html.count(code) == 1 for code in codes)
    assert html.count("<svg") == count
    assert "Scannez le QR code, ou ouvrez agaura.org/K7M4X2 et saisissez ce code." in html
    assert " style=" not in html


@pytest.mark.parametrize(("count", "pages"), [(1, 1), (11, 2)])
def test_download(
    admin: TestClient,
    world: World,
    owner: User,
    caplog: pytest.LogCaptureFixture,
    count: int,
    pages: int,
) -> None:
    public_id = world.assembly(owner, mode=AccessMode.CODES, in_session=False)
    caplog.set_level(logging.DEBUG)
    response = post(admin, f"/manage/{public_id}/codes", {"count": str(count), "output": "pdf"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    with world.database.read() as db:
        batch = access.list_code_batches(db, owner, public_id)[0]
    assert batch.size == count
    assert response.headers["content-disposition"] == (
        f'attachment; filename="agaura-{public_id}-codes-{batch.id}.pdf"'
    )
    assert response.content.startswith(b"%PDF-") and pdf_pages(response.content) == pages
    assert not CODE.search(caplog.text)


def test_codes_are_never_stored_or_logged(
    admin: TestClient,
    world: World,
    owner: User,
    database_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    caplog.set_level(logging.DEBUG)
    page = post(admin, f"/manage/{public_id}/codes", {"count": "5", "output": "page"})
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    codes = CODE.findall(page.text)
    assert len(set(codes)) == 5
    pdf = post(admin, f"/manage/{public_id}/codes", {"count": "5", "output": "pdf"})
    assert pdf.status_code == 200
    world.join(public_id, codes[0])  # the codes work
    stored = b"".join(
        path.read_bytes() for path in database_path.parent.glob(f"{database_path.name}*")
    )
    for code in codes:
        assert code.encode() not in stored
        assert normalize_secret(code).encode() not in stored
        assert code not in caplog.text and normalize_secret(code) not in caplog.text


def test_page_is_still_the_default_without_choice(
    admin: TestClient, world: World, owner: User
) -> None:
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    response = post(admin, f"/manage/{public_id}/codes", {"count": "2"})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    assert len(set(CODE.findall(response.text))) == 2


def test_console_offers_the_pdf_first(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    page = admin.get(f"/manage/{public_id}").text
    pdf = page.index('name="output" value="pdf"')
    assert pdf < page.index('name="output" value="page"')  # Enter submits the first: the PDF
    assert "10 cartes par page A4, avec traits de coupe" in page


@pytest.mark.parametrize("count", ["0", "abc", "2001"])
def test_invalid_counts(admin: TestClient, world: World, owner: User, count: str) -> None:
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    response = post(admin, f"/manage/{public_id}/codes", {"count": count, "output": "pdf"})
    assert response.status_code == 303
    with world.database.read() as db:
        assert access.list_code_batches(db, owner, public_id) == []
