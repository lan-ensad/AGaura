"""PDF documents (code cards, minutes) rendered with WeasyPrint from Jinja templates.

Rendering never touches the network or the file system: only ``data:`` URIs are fetched, so
text typed by an account holder (titles, options) cannot make the server load anything. Render
outside any write transaction: it takes a noticeable fraction of a second, and up to about
20 seconds and 550 MiB for 2,000 code cards.

One document at a time in the whole process (``render_slot``). WeasyPrint lays pages out in
Python, holding the GIL: two renders at once finish no sooner than one after the other, double
the memory, and leave less of the GIL to votes and real-time streams. A request waits at most
``RENDER_WAIT`` seconds for the slot, since a waiting request holds a worker thread that votes
also need; past that, ``PdfBusyError`` (answered 503 with ``Retry-After``, ``agaura.main``).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from weasyprint import HTML
from weasyprint.urls import URLFetcher

from agaura.web.templating import templates

CONCURRENT_RENDERS = 1
RENDER_WAIT = 3.0  # seconds: a minutes render, or a few hundred code cards, ends within it
RETRY_AFTER = 10  # seconds, suggested to the client of a 503

_slots = threading.BoundedSemaphore(CONCURRENT_RENDERS)
_thread = threading.local()


class PdfBusyError(Exception):
    """Another document kept the renderer busy for ``RENDER_WAIT`` seconds."""


@contextmanager
def render_slot() -> Iterator[None]:
    """Hold the renderer. Re-entrant within a thread: a route can reserve it before preparing a
    document (generating codes, drawing their QR codes) and render inside."""
    if getattr(_thread, "holds_slot", False):
        yield
        return
    if not _slots.acquire(timeout=RENDER_WAIT):
        raise PdfBusyError
    _thread.holds_slot = True
    try:
        yield
    finally:
        _thread.holds_slot = False
        _slots.release()


class OfflineFetcher(URLFetcher):
    """Refuse every URL except inline ``data:`` resources."""

    def __init__(self) -> None:
        super().__init__(allowed_protocols={"data"}, allow_redirects=False)


def html_to_pdf(html: str) -> bytes:
    with render_slot():
        pdf = HTML(string=html, url_fetcher=OfflineFetcher()).write_pdf()
    assert pdf is not None  # noqa: S101 (write_pdf without a target returns the bytes)
    return pdf


def render_pdf(template: str, context: dict[str, Any]) -> bytes:
    """Render ``template`` (a Jinja template with its own inline ``<style>``) to PDF bytes."""
    return html_to_pdf(templates.env.get_template(template).render(context))
