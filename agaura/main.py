"""ASGI entry point: ``uvicorn agaura.main:app``."""

import asyncio
import contextlib
import ipaddress
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from agaura import __version__, backups, logs
from agaura.config import Settings, get_settings
from agaura.db import Database
from agaura.i18n import N_, _
from agaura.pdf import RETRY_AFTER, PdfBusyError
from agaura.security import PASSWORD_BUSY_RETRY_AFTER, Keyring, PasswordBusyError
from agaura.services import assemblies
from agaura.services.errors import NotFoundError
from agaura.tasks import start_background_jobs
from agaura.web import account, live, manage, public, results, screen, superadmin
from agaura.web.deps import LoginRequiredError, SuperAdminRefusedError
from agaura.web.manage.common import DelegateEndedError, TooManyRendersError
from agaura.web.realtime import Hub
from agaura.web.security import RateLimiter, SecurityMiddleware
from agaura.web.templating import STATIC_DIR, render

ERROR_PAGES = {
    403: (
        N_("Access refused"),
        N_("The page expired or the request was refused. Go back, reload the page and try again."),
    ),
    404: (N_("Page not found"), N_("This page does not exist or is not available.")),
    429: (N_("Too many attempts"), N_("Too many attempts. Wait a few minutes and try again.")),
    405: (N_("Page not found"), N_("This page does not exist or is not available.")),
}
# PDF documents (minutes, code cards): one at a time in the process, a few per account.
PDF_BUSY = (
    N_("Server busy"),
    N_("Another PDF document is being generated. Try again in a moment."),
)
PDF_TOO_MANY = (
    N_("Too many documents"),
    N_("Too many PDF documents were requested in a short time. Wait a few minutes and try again."),
)
# Passwords: a few hashed or verified at a time in the process (``agaura.security``).
PASSWORD_BUSY = (
    N_("Server busy"),
    N_("Too many passwords are being checked at this moment. Try again in a few seconds."),
)


# Public file: it lists no secret path. Only the pages behind an account are kept from crawling;
# every other page stays crawlable so that engines see its "noindex" header
# (agaura.web.security.NOINDEX_HEADER), the only way to keep a linked URL out of their results.
ROBOTS_TXT = """\
# AGaura: only the home page belongs in search results; the other pages answer
# "X-Robots-Tag: noindex".
User-agent: *
Disallow: /manage/
Disallow: /account
Disallow: /superadmin
Disallow: /dashboard
"""


def _from_loopback(request: Request) -> bool:
    """A request made inside the container: the Docker healthcheck or an operator's command.
    Through Caddy, the client is the visitor's own address (uvicorn trusts the forwarded
    address only from private networks), never a loopback one."""
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _last_backup(settings: Settings) -> str:
    """Time of the latest automatic backup (UTC), "none" before the first, "off" when not
    configured, "unreadable" when its directory cannot be listed."""
    if settings.backup_dir is None:
        return "off"
    try:
        found = backups.list_backups(settings.backup_dir)
    except OSError:
        return "unreadable"
    return f"{found[-1].taken_at:%Y-%m-%dT%H:%M:%SZ}" if found else "none"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    database = Database(settings.sqlalchemy_url, audit_key=settings.audit_key)
    hub = Hub()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # All state lives in the database: after a restart, sessions, polls and timers resume.
        await run_in_threadpool(database.upgrade)
        hub.bind(asyncio.get_running_loop())
        jobs = start_background_jobs(settings, database, hub)
        try:
            yield
        finally:
            hub.close()
            for job in jobs:
                job.cancel()
            for job in jobs:
                with contextlib.suppress(asyncio.CancelledError):
                    await job
            database.dispose()

    # Interactive API docs and the schema they read are a development convenience only.
    docs_url = None if settings.is_production else "/docs"
    openapi_url = None if settings.is_production else "/openapi.json"
    app = FastAPI(
        title="AGaura",
        version=__version__,
        docs_url=docs_url,
        redoc_url=None,
        openapi_url=openapi_url,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.keyring = Keyring(settings.secret_key.get_secret_value())
    app.state.hub = hub
    app.state.limiter = RateLimiter()
    app.add_middleware(
        SecurityMiddleware, secure_cookies=settings.is_production, hsts=settings.is_production
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/robots.txt", include_in_schema=False)
    def robots() -> PlainTextResponse:
        return PlainTextResponse(ROBOTS_TXT)

    @app.get("/health")
    def health(request: Request) -> dict[str, str | int]:
        # The database answers for everyone. The version, the sessions in session (checked
        # before a deployment, SPEC §10) and the latest automatic backup only for a request from
        # inside the container: the outside world learns neither which release runs nor when
        # assemblies take place.
        with database.read() as db:
            in_session = assemblies.count_in_session(db)
        if not _from_loopback(request):
            return {"status": "ok"}
        return {
            "status": "ok",
            "version": __version__,
            "sessions_in_session": in_session,
            "last_backup": _last_backup(settings),
        }

    @app.exception_handler(LoginRequiredError)
    async def login_required(_request: Request, error: LoginRequiredError) -> Response:
        return RedirectResponse(f"/login?next={error.next_url}", status_code=303)

    @app.exception_handler(SuperAdminRefusedError)
    async def superadmin_refused(request: Request, _error: SuperAdminRefusedError) -> Response:
        return render(request, "superadmin/refused.html", status_code=403)

    @app.exception_handler(DelegateEndedError)
    async def delegate_ended(request: Request, error: DelegateEndedError) -> Response:
        # The cookie stays: the console scripts reload into this page, which offers to remove it.
        public_id = assemblies.normalize_public_id(error.public_id) or error.public_id
        context = {"public_id": public_id}
        response = render(request, "manage/delegate_ended.html", context, status_code=403)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(PdfBusyError)
    async def pdf_busy(request: Request, _error: PdfBusyError) -> Response:
        response = error_page(request, 503, PDF_BUSY)
        response.headers["Retry-After"] = str(RETRY_AFTER)
        return response

    @app.exception_handler(PasswordBusyError)
    async def password_busy(request: Request, _error: PasswordBusyError) -> Response:
        response = error_page(request, 503, PASSWORD_BUSY)
        response.headers["Retry-After"] = str(PASSWORD_BUSY_RETRY_AFTER)
        return response

    @app.exception_handler(TooManyRendersError)
    async def too_many_renders(request: Request, _error: TooManyRendersError) -> Response:
        return error_page(request, 429, PDF_TOO_MANY)

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, _error: NotFoundError) -> Response:
        return error_page(request, 404)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: HTTPException) -> Response:
        if request.url.path.startswith(("/static/", "/docs", "/openapi")):
            return Response(status_code=error.status_code)
        return error_page(request, error.status_code)

    # Fixed routes first; the voter routes (``/{session_id}``) catch what is left. The screen and
    # public results routes (``/{session_id}/screen/…``, ``/{session_id}/results/…``) come before
    # them for clarity, although their paths never collide.
    app.include_router(public.router)
    app.include_router(account.router)
    app.include_router(superadmin.router)
    app.include_router(manage.entry_router)  # delegate links: no account (FR-92)
    app.include_router(manage.router)
    app.include_router(screen.router)
    app.include_router(results.router)
    app.include_router(live.router)
    return app


def error_page(
    request: Request, status_code: int, texts: tuple[str, str] | None = None
) -> Response:
    title, text = texts or ERROR_PAGES.get(status_code, ERROR_PAGES[404])
    return render(
        request,
        "error.html",
        {"title": _(title), "message": _(text)},
        status_code=status_code,
    )


def _served_app() -> FastAPI:
    """The application uvicorn imports (``agaura.main:app``). Logging is set here, once per
    process, rather than in ``create_app``, which tests call with production settings too."""
    settings = get_settings()
    if settings.is_production:
        logs.configure()  # JSON lines on standard output (SPEC §10)
    return create_app(settings)


app = _served_app()
