"""Shared FastAPI dependencies: application objects, the current account, CSRF.

Three kinds of logged-in pages: ``CurrentUser`` (account holders: dashboard, console; a
super-admin is refused with a 403 page before anything is read), ``SuperAdmin`` (``/superadmin``;
everyone else gets the 404 page) and ``LoggedInUser`` (either, e.g. ``/account``).
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, Path, Request

from agaura.config import Settings
from agaura.db import Database
from agaura.models import User
from agaura.security import Keyring
from agaura.services import auth
from agaura.services.errors import NotFoundError
from agaura.web.realtime import Hub
from agaura.web.security import (
    SESSION_COOKIE,
    RateLimiter,
    read_cookie,
    refuse_cross_site,
    verify_csrf,
)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_keyring(request: Request) -> Keyring:
    return request.app.state.keyring


def get_hub(request: Request) -> Hub:
    return request.app.state.hub


def get_limiter(request: Request) -> RateLimiter:
    return request.app.state.limiter


class LoginRequiredError(Exception):
    """Raised by ``current_user``; turned into a redirect to the login page."""

    def __init__(self, next_url: str) -> None:
        super().__init__(next_url)
        self.next_url = next_url


def optional_user(request: Request) -> User | None:
    """The logged-in account, or None. Also stored as ``request.state.user`` for templates."""
    if hasattr(request.state, "user"):
        return request.state.user
    database: Database = request.app.state.database
    token = read_cookie(request, SESSION_COOKIE)
    with database.read() as db:
        found = auth.lookup(db, token)
    if found is not None and found.needs_touch and token:
        with database.write() as db:
            auth.touch(db, token)
    request.state.user = found.user if found else None
    return request.state.user


class SuperAdminRefusedError(Exception):
    """Raised by ``current_user`` for a super-admin; turned into a 403 page."""


def logged_in_user(request: Request) -> User:
    """The logged-in account, super-admin or not; anonymous visitors are redirected to the
    login page."""
    user = optional_user(request)
    if user is None:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise LoginRequiredError(quote(target, safe="/?=&"))
    return user


def current_user(request: Request) -> User:
    """The logged-in account holder. The super-admin never reaches sessions or their content:
    it is refused here, before any session is looked up, so the answer is the same for every
    path."""
    user = logged_in_user(request)
    if user.is_superadmin:
        raise SuperAdminRefusedError
    return user


def superadmin_user(request: Request) -> User:
    """The logged-in super-admin; other accounts get the 404 page."""
    user = logged_in_user(request)
    if not user.is_superadmin:
        raise NotFoundError("page")
    return user


def landing_page(user: User) -> str:
    """Where an account goes after logging in."""
    return "/superadmin" if user.is_superadmin else "/dashboard"


def public_base_url(request: Request) -> str:
    """Base of the addresses shown to voters (QR codes, links).

    In production, the configured address. In development, the address used to reach the
    console, so that a phone on the same network can open the links.
    """
    settings: Settings = request.app.state.settings
    if settings.is_production:
        return settings.base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


# A row id in a URL path: past SQLite's integer range the query would raise (an error page 500)
# instead of finding nothing; refused like any malformed path parameter.
RowId = Annotated[int, Path(le=2**63 - 1)]

CurrentUser = Annotated[User, Depends(current_user)]
LoggedInUser = Annotated[User, Depends(logged_in_user)]
SuperAdmin = Annotated[User, Depends(superadmin_user)]
OptionalUser = Annotated[User | None, Depends(optional_user)]
DatabaseDep = Annotated[Database, Depends(get_database)]
KeyringDep = Annotated[Keyring, Depends(get_keyring)]
HubDep = Annotated[Hub, Depends(get_hub)]
LimiterDep = Annotated[RateLimiter, Depends(get_limiter)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
CsrfProtected = Depends(verify_csrf)
SameSiteOnly = Depends(refuse_cross_site)
