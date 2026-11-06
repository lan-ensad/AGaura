"""PDF rendering fetches nothing outside the document and renders one document at a time
(agaura.pdf)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from agaura import pdf
from agaura.pdf import OfflineFetcher, PdfBusyError, html_to_pdf, render_slot


def test_renders_french_text_without_fetching_anything(tmp_path: Path) -> None:
    secret = tmp_path / "secret.css"
    secret.write_text("h1 { color: red }")
    html = (
        "<h1>Procès-verbal</h1>"
        f'<link rel="stylesheet" href="file://{secret}">'
        '<img src="http://127.0.0.1:9/never.png">'
        "<img src=\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg'/>\">"
    )
    pdf = html_to_pdf(html)
    assert pdf.startswith(b"%PDF-")


def test_the_fetcher_refuses_files_and_network() -> None:
    fetcher = OfflineFetcher()
    for url in ("file:///etc/hostname", "http://127.0.0.1:9/x.png", "https://example.org/a.css"):
        with pytest.raises(ValueError, match="disallowed protocol"):
            fetcher.fetch(url)
    assert fetcher.fetch("data:text/plain,ok").read() == b"ok"


def test_one_render_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf, "RENDER_WAIT", 0.1)
    held, release = threading.Event(), threading.Event()

    def other_request() -> None:
        with render_slot():
            held.set()
            release.wait(5)

    thread = threading.Thread(target=other_request)
    thread.start()
    try:
        assert held.wait(5)
        start = time.monotonic()
        with pytest.raises(PdfBusyError):
            html_to_pdf("<p>Procès-verbal</p>")
        assert time.monotonic() - start < 1
    finally:
        release.set()
        thread.join()
    assert html_to_pdf("<p>Procès-verbal</p>").startswith(b"%PDF-")


def test_the_slot_is_reentrant_within_a_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """A route reserves the renderer before preparing the document, then renders inside."""
    monkeypatch.setattr(pdf, "RENDER_WAIT", 0.1)
    with render_slot():
        assert html_to_pdf("<p>Cartes</p>").startswith(b"%PDF-")
    with render_slot():  # released on the way out
        pass
