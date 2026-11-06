import asyncio
import json
import logging
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agaura import __version__, backups
from agaura.config import (
    DEVELOPMENT_SECRET_KEY,
    EmailBackend,
    Environment,
    Settings,
    is_local_host,
)
from agaura.db import Database
from agaura.main import _last_backup, create_app
from agaura.security import Keyring
from agaura.services import demo, outbox
from agaura.tasks import close_expired_polls, make_sender, purge_demos, run_periodically
from tests.conftest import TEST_SECRET_KEY


def test_health_reports_ok_and_sessions_in_session(settings: Settings) -> None:
    """Details only from inside the container (the deployment check, the Docker healthcheck)."""
    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "sessions_in_session": 0,
        "last_backup": "off",
    }
    assert (settings.data_dir / "agaura.db").exists()


def test_last_backup_states(settings: Settings, tmp_path: Path) -> None:
    assert _last_backup(settings) == "off"
    directory = tmp_path / "auto"
    configured = settings.model_copy(update={"backup_dir": directory})
    assert _last_backup(configured) == "unreadable"
    directory.mkdir()
    assert _last_backup(configured) == "none"
    (directory / "agaura-20261105-143000Z.db").write_bytes(b"")
    assert _last_backup(configured) == "2026-11-05T14:30:00Z"


def test_the_application_backs_up_from_its_start(settings: Settings, tmp_path: Path) -> None:
    """The background job writes the first copy at once; /health tells the operator, from
    inside the container, when the latest one was taken."""
    directory = tmp_path / "auto"
    settings = settings.model_copy(update={"backup_dir": directory})
    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and client.get("/health").json()["last_backup"] in (
            "unreadable",
            "none",
        ):
            time.sleep(0.05)
        reported = client.get("/health").json()["last_backup"]
    (copy,) = backups.list_backups(directory)
    assert [entry.name for entry in directory.iterdir()] == [copy.path.name]
    assert reported == f"{copy.taken_at:%Y-%m-%dT%H:%M:%SZ}"


def test_one_version_number_everywhere() -> None:
    """``deploy/update.sh`` installs the tag ``v<version>`` and then expects /health to report
    ``<version>``; the image installs with ``uv sync --locked``, which refuses a stale lock."""
    root = Path(__file__).resolve().parent.parent
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    locked = next(package["version"] for package in lock["package"] if package["name"] == "agaura")
    assert project["version"] == locked == __version__


@pytest.mark.parametrize("address", ["203.0.113.9", "172.18.0.4", "2001:db8::1", "testclient"])
def test_health_tells_the_outside_world_nothing_more(settings: Settings, address: str) -> None:
    with TestClient(create_app(settings), client=(address, 50000)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_robots_txt_lists_no_secret_path(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/robots.txt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "User-agent: *" in response.text and "Disallow: /manage/" in response.text
    for secret in ("/screen", "/results", "/verify", "/reset", "/invite"):
        assert secret not in response.text


def test_only_the_home_page_may_be_indexed(settings: Settings) -> None:
    """A noindex header, not robots.txt, keeps a linked URL (and its token) out of search
    results: the page must stay crawlable for the header to be seen."""
    with TestClient(create_app(settings)) as client:
        assert "x-robots-tag" not in client.get("/").headers
        assert "x-robots-tag" not in client.get("/robots.txt").headers
        for path in ("/login", "/ABCDEF", "/ABCDEF/screen/some-token", "/static/css/app.css"):
            assert client.get(path).headers["x-robots-tag"] == "noindex, nofollow", path


def test_api_docs_hidden_in_production(settings: Settings) -> None:
    production = settings.model_copy(update={"environment": Environment.PRODUCTION})
    with TestClient(create_app(production)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_api_docs_available_in_development(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200


class TestSettings:
    @pytest.mark.parametrize("key", [DEVELOPMENT_SECRET_KEY, "too-short"])
    def test_production_requires_a_real_secret_key(self, key: str) -> None:
        with pytest.raises(ValueError, match="AGAURA_SECRET_KEY"):
            Settings(environment=Environment.PRODUCTION, secret_key=key, _env_file=None)  # type: ignore[call-arg]

    def test_production_accepts_a_long_secret_key(self) -> None:
        settings = Settings(
            environment=Environment.PRODUCTION,
            secret_key=TEST_SECRET_KEY,
            _env_file=None,  # type: ignore[call-arg]
        )
        assert settings.is_production

    @pytest.mark.parametrize(
        ("base_url", "domain"),
        [
            ("https://agaura.org", "localhost"),
            ("http://localhost:8000", "agaura.org"),
            ("http://203.0.114.5:8000", "localhost"),
        ],
    )
    def test_development_is_refused_on_a_public_address(self, base_url: str, domain: str) -> None:
        with pytest.raises(ValueError, match="AGAURA_ENVIRONMENT=production"):
            Settings(base_url=base_url, domain=domain, _env_file=None)  # type: ignore[call-arg]

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://localhost:8000",
            "https://localhost",
            "http://127.0.0.1:8000",
            "http://192.168.1.42:8000",
            "http://[::1]:8000",
            "http://agaura.local",
            "http://laptop:8000",
        ],
    )
    def test_development_runs_on_a_local_address(self, base_url: str) -> None:
        settings = Settings(base_url=base_url, _env_file=None)  # type: ignore[call-arg]
        assert not settings.is_production

    def test_production_accepts_a_public_address(self) -> None:
        settings = Settings(
            environment=Environment.PRODUCTION,
            secret_key=TEST_SECRET_KEY,
            base_url="https://agaura.org/",
            domain="agaura.org",
            email_backend=EmailBackend.SMTP,
            _env_file=None,  # type: ignore[call-arg]
        )
        assert settings.is_production

    @pytest.mark.parametrize(
        ("base_url", "domain"),
        [
            ("https://localhost", "agaura.org"),  # .env.example's value left in place
            ("http://agaura.org", "agaura.org"),
            ("https://www.agaura.org", "agaura.org"),
            ("https://agaura.org", "localhost"),
        ],
    )
    def test_production_refuses_a_base_url_other_than_the_domain(
        self, base_url: str, domain: str
    ) -> None:
        """Emails and the QR codes of code cards carry this address (``public_base_url``)."""
        with pytest.raises(ValueError, match="AGAURA_BASE_URL"):
            Settings(
                environment=Environment.PRODUCTION,
                secret_key=TEST_SECRET_KEY,
                base_url=base_url,
                domain=domain,
                email_backend=EmailBackend.SMTP,
                _env_file=None,  # type: ignore[call-arg]
            )

    def test_production_refuses_the_console_email_backend_on_a_public_address(self) -> None:
        """Nothing would leave: invitations, password resets and D12 warnings would vanish."""
        with pytest.raises(ValueError, match="AGAURA_EMAIL_BACKEND=console"):
            Settings(
                environment=Environment.PRODUCTION,
                secret_key=TEST_SECRET_KEY,
                base_url="https://agaura.org",
                domain="agaura.org",
                email_backend=EmailBackend.CONSOLE,
                _env_file=None,  # type: ignore[call-arg]
            )

    def test_a_production_trial_on_this_machine_keeps_the_console_backend(self) -> None:
        """Docker in production on https://localhost, as in the restore drill (docs/INSTALL.md)."""
        settings = Settings(
            environment=Environment.PRODUCTION,
            secret_key=TEST_SECRET_KEY,
            base_url="https://localhost:8443",
            domain="localhost",
            _env_file=None,  # type: ignore[call-arg]
        )
        assert settings.email_backend is EmailBackend.CONSOLE

    @pytest.mark.parametrize(
        ("host", "local"),
        [
            ("localhost", True),
            ("app.localhost", True),
            ("LOCALHOST.", True),
            ("printer.local", True),
            ("agaura.test", True),
            ("laptop", True),
            ("10.0.0.7", True),
            ("100.64.1.2", True),
            ("fe80::1", True),
            ("[fd00::1]", True),
            ("", True),
            ("agaura.org", False),
            ("www.agaura.org", False),
            ("8.8.8.8", False),
            ("2a01:4f8::1", False),
        ],
    )
    def test_local_hosts(self, host: str, local: bool) -> None:
        assert is_local_host(host) is local

    @pytest.mark.parametrize("sender", ["AGaura <no-reply@agaura.org>", "no-reply@agaura.org"])
    def test_email_from_accepts_a_sender(self, sender: str) -> None:
        settings = Settings(email_from=sender, _env_file=None)  # type: ignore[call-arg]
        assert settings.email_from == sender

    @pytest.mark.parametrize(
        "sender", ["AGaura <no-reply@agaura.org>\nBcc: x@y.org", "AGaura", "no-reply"]
    )
    def test_email_from_refuses_what_cannot_send(self, sender: str) -> None:
        with pytest.raises(ValueError, match="AGAURA_EMAIL_FROM"):
            Settings(email_from=sender, _env_file=None)  # type: ignore[call-arg]

    def test_database_defaults_to_the_data_directory(self, settings: Settings) -> None:
        assert settings.sqlalchemy_url == f"sqlite:///{settings.data_dir / 'agaura.db'}"
        explicit = settings.model_copy(update={"database_url": "sqlite:///elsewhere.db"})
        assert explicit.sqlalchemy_url == "sqlite:///elsewhere.db"


class TestBackgroundJobs:
    def test_sender_follows_the_settings(self, settings: Settings) -> None:
        assert isinstance(make_sender(settings), outbox.ConsoleSender)
        smtp = settings.model_copy(update={"email_backend": EmailBackend.SMTP})
        sender = make_sender(smtp)
        assert isinstance(sender, outbox.SmtpSender)
        assert sender.host == "mail.infomaniak.com"

    def test_poll_timer_job_runs_on_an_empty_database(self, database: Database) -> None:
        assert close_expired_polls(database) == 0

    def test_demo_purge_announces_the_deleted_sessions(
        self, database: Database, keyring: Keyring
    ) -> None:
        """Voter pages, screens and delegate consoles of a purged demo (FR-09a) recheck their
        access at once."""
        with database.write() as db:
            demo.start(db, keyring, limit=1)  # never logged in: purged at the next run
        published: list[tuple[str, str]] = []

        class Recorder:
            def publish(self, channel: str, event: str, data: str = "") -> None:
                published.append((channel, event))

        assert purge_demos(database, Recorder()) == 2  # type: ignore[arg-type]
        assert len([channel for channel, _ in published if channel.startswith("live:")]) == 2
        assert len([channel for channel, _ in published if channel.startswith("manage:")]) == 2
        assert purge_demos(database) == 0

    def test_a_failing_job_is_logged_and_retried(self, caplog: pytest.LogCaptureFixture) -> None:
        calls: list[int] = []

        def job() -> None:
            calls.append(1)
            raise RuntimeError("boom")

        async def run() -> None:
            task = asyncio.create_task(run_periodically("test", 0.01, job))
            while len(calls) < 3:
                await asyncio.sleep(0.01)
            task.cancel()

        with caplog.at_level(logging.ERROR):
            asyncio.run(run())
        assert len(calls) >= 3
        assert "background job test failed" in caplog.text


def test_production_logs_are_json_lines() -> None:
    """In a process of its own: the configuration replaces the handlers of the process."""
    script = (
        "import logging\n"
        "from agaura import logs\n"
        "logs.configure()\n"
        "logging.getLogger('agaura.backups').info('backup written: %s', 'agaura-x.db')\n"
        "logging.getLogger('uvicorn.error').info('Application startup complete.')\n"
        "logging.getLogger('agaura.tasks').debug('not shown')\n"
        "try:\n"
        "    1 / 0\n"
        "except ZeroDivisionError:\n"
        "    logging.getLogger('agaura.tasks').exception('background job failed')\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(line["level"], line["logger"], line["message"]) for line in lines] == [
        ("INFO", "agaura.backups", "backup written: agaura-x.db"),
        ("INFO", "uvicorn.error", "Application startup complete."),
        ("ERROR", "agaura.tasks", "background job failed"),
    ]
    assert lines[0]["time"].endswith("+00:00")
    assert "ZeroDivisionError" in lines[2]["exception"]
    assert result.stderr == ""
