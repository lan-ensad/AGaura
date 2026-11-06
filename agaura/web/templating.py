"""Jinja2 templates with French translations, flash messages and asset URLs.

Templates live in ``agaura/templates/`` and never contain French text directly: every visible
string goes through ``_()`` (see ``agaura/i18n.py``).
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from functools import cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from babel.dates import format_date, format_datetime
from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from starlette.responses import HTMLResponse, Response

from agaura import __version__
from agaura.i18n import DEFAULT_LOCALE, _, ngettext
from agaura.web.labels import TEMPLATE_GLOBALS
from agaura.web.messages import NOTICES, error_message
from agaura.web.security import FLASH_COOKIE, delete_cookie, read_cookie, set_cookie

TEMPLATE_DIR = Path(__file__).parents[1] / "templates"
STATIC_DIR = Path(__file__).parents[1] / "static"
DISPLAY_TIMEZONE = ZoneInfo("Europe/Paris")


@cache
def static_url(path: str) -> str:
    """URL of a static file, with a content hash so browsers never keep a stale copy."""
    digest = hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:10]
    return f"/static/{path}?v={digest}"


def long_date(value: date | None) -> str:
    return "" if value is None else format_date(value, format="long", locale=DEFAULT_LOCALE)


def short_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return format_datetime(
        value, format="d MMM y, HH:mm", tzinfo=DISPLAY_TIMEZONE, locale=DEFAULT_LOCALE
    )


def time_of_day(value: datetime | None) -> str:
    if value is None:
        return ""
    return format_datetime(value, format="HH:mm", tzinfo=DISPLAY_TIMEZONE, locale=DEFAULT_LOCALE)


def clock_time(value: datetime | None) -> str:
    """A time to the second (``13:41:48``): when a poll closes by itself, a minute too early
    would read as already closed."""
    if value is None:
        return ""
    return format_datetime(value, format="HH:mm:ss", tzinfo=DISPLAY_TIMEZONE, locale=DEFAULT_LOCALE)


def percent(value: Any) -> str:
    """Format a share (Fraction, "n/d" string or float) as a French percentage."""
    if value is None:
        return "\u2013"
    if isinstance(value, str) and "/" in value:
        numerator, denominator = (int(part) for part in value.split("/"))
        value = numerator / denominator if denominator else 0
    return f"{float(value) * 100:.1f} %".replace(".", ",")


def make_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml", "svg"]),
        extensions=["jinja2.ext.i18n"],
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.install_gettext_callables(_, ngettext, newstyle=True)  # type: ignore[attr-defined]
    env.globals.update(static_url=static_url, version=__version__, **TEMPLATE_GLOBALS)
    env.filters.update(
        long_date=long_date,
        short_datetime=short_datetime,
        time_of_day=time_of_day,
        clock_time=clock_time,
        percent=percent,
    )
    return env


templates = Jinja2Templates(env=make_environment())


def flash(response: Response, request: Request, code: str) -> None:
    """Show a notice (see ``NOTICES``) on the next page rendered for this browser."""
    set_cookie(response, request, FLASH_COOKIE, code, max_age=60)


def flash_error(response: Response, request: Request, code: str) -> None:
    """Show an error message (see ``ERRORS``) on the next page rendered for this browser."""
    set_cookie(response, request, FLASH_COOKIE, f"error:{code}", max_age=60)


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    code = read_cookie(request, FLASH_COOKIE) or ""
    notice = NOTICES.get(code)
    flashed_error = error_message(code[6:]) if code.startswith("error:") else None
    full_context = {
        "csrf_token": request.state.csrf_token,
        "user": getattr(request.state, "user", None),
        # Set by the console when it is reached through a delegate link (FR-92): top bar.
        "console_access": getattr(request.state, "console_access", None),
        "notice": _(notice) if notice else None,
        "error": flashed_error,
        **(context or {}),
    }
    response = templates.TemplateResponse(request, name, full_context, status_code=status_code)
    if code:
        delete_cookie(response, request, FLASH_COOKIE)
    return response
