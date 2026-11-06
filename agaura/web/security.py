"""HTTP hardening (SPEC §9.3): security headers, CSRF protection, rate limiting, cookies."""

from __future__ import annotations

import contextlib
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator, Mapping
from http.cookies import SimpleCookie

from fastapi import HTTPException, Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agaura.security import new_token, secrets_match

# Cookies, by their name in development. Every cookie is ``Secure`` in production, and there its
# name carries the prefix that makes browsers enforce its attributes (``cookie_name``):
# ``__Host-`` for the ones set on ``Path=/`` without ``Domain`` (this host only, never set by a
# subdomain or over plain HTTP), ``__Secure-`` for the ones scoped to the pages of one session.
SESSION_COOKIE = "agaura_session"  # login session (``deps.optional_user``)
CSRF_COOKIE = "agaura_csrf"
FLASH_COOKIE = "agaura_flash"  # notice for the next page (``templating.flash``)
VOTER_COOKIE = "agaura_voter"  # path /{ID}: the device credential of a voter
DELEGATE_COOKIE = "agaura_delegate"  # path /manage/{ID}: the token of a delegate link
PATH_SCOPED_COOKIES = frozenset({VOTER_COOKIE, DELEGATE_COOKIE})

CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"

# Strict CSP: everything is served by the application, no inline script or style.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    ]
)
SECURITY_HEADERS = [
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
]
# Only the home page belongs in search results. Every other answer (session pages, screen and
# public results links with their secret token, console, static files) asks search engines not
# to index it. A header rather than robots.txt: a URL that robots.txt forbids is never fetched,
# so an engine that finds it linked elsewhere may list it, token included, without ever seeing
# a "noindex" (agaura/main.py, robots.txt).
INDEXABLE_PATHS = frozenset({"/", "/robots.txt"})
NOINDEX_HEADER = (b"x-robots-tag", b"noindex, nofollow")
# Only meaningful over HTTPS (browsers ignore, and complain about, COOP on plain HTTP).
HTTPS_HEADERS = [
    (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
    (b"cross-origin-opener-policy", b"same-origin"),
]


def cookie_name(name: str, *, secure: bool) -> str:
    """The name the cookie ``name`` goes by in the browser. Both prefixes require the
    ``Secure`` attribute, which development over plain HTTP leaves out: there the name stays
    as is."""
    if not secure:
        return name
    return ("__Secure-" if name in PATH_SCOPED_COOKIES else "__Host-") + name


def _secure(request: Request) -> bool:
    return request.app.state.settings.is_production


def read_cookie(request: Request, name: str) -> str | None:
    """The value of the cookie ``name`` sent with ``request``, under its name in this
    environment (a cookie without the production prefix is ignored there)."""
    return request.cookies.get(cookie_name(name, secure=_secure(request)))


def _cookies(scope: Scope) -> SimpleCookie:
    cookie = SimpleCookie()
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            with contextlib.suppress(Exception):  # a malformed cookie header is ignored
                cookie.load(value.decode("latin-1"))
    return cookie


class SecurityMiddleware:
    """Pure ASGI middleware (safe with streaming responses).

    Adds the security headers to every response, and makes sure every visitor has a CSRF
    cookie; its value is exposed as ``request.state.csrf_token`` for the forms.
    """

    def __init__(self, app: ASGIApp, *, secure_cookies: bool, hsts: bool) -> None:
        self.app = app
        self.secure_cookies = secure_cookies
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        name = cookie_name(CSRF_COOKIE, secure=self.secure_cookies)
        morsel = _cookies(scope).get(name)
        token = morsel.value if morsel is not None and len(morsel.value) >= 32 else None
        new_cookie = token is None
        token = token or new_token()
        scope.setdefault("state", {})["csrf_token"] = token

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend(h for h in SECURITY_HEADERS if h[0] not in present)
                if scope["path"] not in INDEXABLE_PATHS and NOINDEX_HEADER[0] not in present:
                    headers.append(NOINDEX_HEADER)
                if self.hsts:
                    headers.extend(HTTPS_HEADERS)
                if new_cookie:
                    attributes = "Path=/; HttpOnly; SameSite=Lax"
                    if self.secure_cookies:
                        attributes += "; Secure"
                    headers.append((b"set-cookie", f"{name}={token}; {attributes}".encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def csrf_token(request: Request) -> str:
    return request.state.csrf_token


async def verify_csrf(request: Request) -> None:
    """Dependency for every state-changing route: form field or header must match the cookie."""
    cookie = read_cookie(request, CSRF_COOKIE)
    sent = request.headers.get(CSRF_HEADER)
    if sent is None and request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        sent = (await request.form()).get(CSRF_FIELD)  # type: ignore[assignment]
    if not cookie or not isinstance(sent, str) or not secrets_match(cookie, sent):
        raise HTTPException(status_code=403, detail="csrf")


def refuse_cross_site(request: Request) -> None:
    """Dependency of the console's downloads (GET): one started from another site's page is
    refused. SameSite=Lax sends the session cookie with a navigation from anywhere, so such a
    page could spend the account's PDF budget and fill the session log with exports. Browsers
    that do not send ``Sec-Fetch-Site`` go through."""
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="cross-site")


def _name_on_path(request: Request, name: str, path: str) -> str:
    """``cookie_name``, once checked that ``path`` suits the prefix: a ``__Host-`` cookie on
    another path than ``/`` would be dropped by browsers in production only."""
    if (path != "/") != (name in PATH_SCOPED_COOKIES):
        msg = f"cookie {name} cannot be set on path {path}"
        raise ValueError(msg)
    return cookie_name(name, secure=_secure(request))


def set_cookie(
    response: Response,
    request: Request,
    name: str,
    value: str,
    *,
    max_age: int | None = None,
    path: str = "/",
) -> None:
    """Set the cookie ``name`` (one of the names above, the prefix is added here)."""
    response.set_cookie(
        _name_on_path(request, name, path),
        value,
        max_age=max_age,
        path=path,
        httponly=True,
        samesite="lax",
        secure=_secure(request),
    )


def delete_cookie(response: Response, request: Request, name: str, *, path: str = "/") -> None:
    response.delete_cookie(
        _name_on_path(request, name, path),
        path=path,
        httponly=True,
        samesite="lax",
        secure=_secure(request),
    )


def client_ip(request: Request) -> str:
    """Client address (uvicorn applies the proxy headers set by Caddy)."""
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """Sliding-window counters kept in memory (one process).

    Typical use: ``charge`` an attempt before the step that depends on a secret (checking a
    password or a code), ``refund`` it when that step succeeds, so legitimate users who succeed
    are never slowed down. ``charge`` checks every limit and counts in one step: concurrent
    requests cannot all pass the check before any of them is counted. ``exceeded`` and
    ``record`` remain for counters that need no such guarantee. Every method holds one
    re-entrant lock; ``atomic`` holds it across several calls, for a check-and-count that
    ``charge`` cannot express (``limits.charge_join_attempt``).

    Memory guard: past ``MAX_KEYS`` keys, the ones recorded least recently are dropped, a tenth
    at a time. A flood of new addresses therefore cannot reset the counters that matter, which
    are the ones being recorded (a session under attack, an address that keeps failing).
    """

    MAX_KEYS = 100_000

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._events: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.RLock()

    def _count(self, key: str, seconds: float) -> int:
        events = self._events.get(key)
        if events is None:
            return 0
        horizon = self._clock() - seconds
        while events and events[0] <= horizon:
            events.popleft()
        if not events:
            self._events.pop(key, None)
        return len(events)

    def _record(self, key: str) -> None:
        events = self._events.get(key)
        if events is None:
            if len(self._events) >= self.MAX_KEYS:
                for _ in range(max(1, self.MAX_KEYS // 10)):
                    self._events.popitem(last=False)
            events = self._events[key] = deque()
        else:
            self._events.move_to_end(key)
        events.append(self._clock())

    @contextlib.contextmanager
    def atomic(self) -> Iterator[None]:
        with self._lock:
            yield

    def count(self, key: str, seconds: float) -> int:
        with self._lock:
            return self._count(key, seconds)

    def exceeded(self, key: str, limit: int, seconds: float) -> bool:
        with self._lock:
            return self._count(key, seconds) >= limit

    def record(self, key: str) -> None:
        with self._lock:
            self._record(key)

    def charge(self, limits: Mapping[str, tuple[int, float]]) -> bool:
        """Count one event on every key of ``limits`` (key: (limit, window in seconds)) unless
        one of them already reached its limit; False, with nothing counted, in that case."""
        with self._lock:
            if any(self._count(key, seconds) >= limit for key, (limit, seconds) in limits.items()):
                return False
            for key in limits:
                self._record(key)
            return True

    def refund(self, *keys: str) -> None:
        """Take back one event of each key, for a charged attempt that was not a failure. The
        latest event goes: concurrent attempts are a few milliseconds apart, so which one does
        not matter."""
        with self._lock:
            for key in keys:
                events = self._events.get(key)
                if events:
                    events.pop()
                if not events:
                    self._events.pop(key, None)

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)
