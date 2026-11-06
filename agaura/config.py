"""Application settings, read from environment variables prefixed with ``AGAURA_``."""

import ipaddress
from email.message import EmailMessage
from email.utils import parseaddr
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agaura.security import Keyring

# Development-only fallback. Production refuses to start with it (see ``Settings``).
DEVELOPMENT_SECRET_KEY = "development-only-secret-key-never-use-in-production"  # noqa: S105
MIN_SECRET_KEY_LENGTH = 32

# Host suffixes that never resolve on the public Internet (RFC 6761, RFC 6762).
LOCAL_SUFFIXES = (".localhost", ".local", ".test", ".internal")


def is_local_host(host: str) -> bool:
    """True for a host only reachable on this machine or a private network: ``localhost``, a
    single-label name, a reserved suffix, or a non-global IP address (loopback, private,
    link-local, carrier-grade NAT)."""
    host = host.strip().strip("[]").rstrip(".").lower()
    if not host:
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    return host == "localhost" or "." not in host or host.endswith(LOCAL_SUFFIXES)


class Environment(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class SignupMode(StrEnum):
    CLOSED = "closed"  # accounts are created by the super-admin, by invitation
    OPEN = "open"  # self-service signup with the anti-bot check (FR-01)


class EmailBackend(StrEnum):
    CONSOLE = "console"  # development: log that an email was sent, never its body
    SMTP = "smtp"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGAURA_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    environment: Environment = Environment.DEVELOPMENT
    base_url: str = "http://localhost:8000"
    # Domain served by Caddy (Docker Compose); read only to refuse development on a public one.
    domain: str = "localhost"
    data_dir: Path = Path("data")
    # SQLAlchemy URL; defaults to an SQLite file in ``data_dir``.
    database_url: str | None = None

    # Master secret: session password encryption, voter code digests and the audit log chain
    # key derive from it. Changing it makes stored session passwords and printed codes unusable,
    # and the audit log fails verification (``agaura audit verify``).
    secret_key: SecretStr = SecretStr(DEVELOPMENT_SECRET_KEY)

    # Public signup; closed by default: the super-admin invites each account.
    signup: SignupMode = SignupMode.CLOSED

    # Public repository of the running code, linked from every page (AGPL, section 13).
    source_url: str | None = None

    # Address to write to for an account while signup is closed, shown on the home page.
    contact_email: str = "contact@agaura.org"

    # Demo (FR-09a), on unless turned off: "Essayer la démo" opens an account of one's own,
    # deleted with its sessions after 2 h without activity and at most 24 h; at most
    # ``demo_max`` such accounts at once.
    demo: bool = True
    demo_max: int = Field(default=50, ge=1)

    email_backend: EmailBackend = EmailBackend.CONSOLE
    email_from: str = "AGaura <no-reply@agaura.org>"
    smtp_host: str = "mail.infomaniak.com"
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")

    # Automatic backups (SPEC §10, agaura/backups.py): a compacted copy of the database in this
    # directory once a day and after each poll closing, kept ``backup_keep_days`` days. Off when
    # unset; /backups in the Docker image (compose.yaml), a directory outside the data volume.
    backup_dir: Path | None = None
    backup_keep_days: int = Field(default=30, ge=1)

    # Seconds between two checks for polls whose timer has run out, and for emails to send.
    poll_timer_interval: float = 5.0
    email_delivery_interval: float = 5.0

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def signup_open(self) -> bool:
        return self.signup == SignupMode.OPEN

    @property
    def sqlalchemy_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'agaura.db'}"

    @property
    def audit_key(self) -> bytes:
        """HMAC key of the audit log chain, derived from ``secret_key`` (``Database``)."""
        return Keyring(self.secret_key.get_secret_value()).audit_key

    @model_validator(mode="after")
    def _refuse_development_on_a_public_address(self) -> Self:
        # Development falls back to the published secret key, non-Secure cookies and API docs:
        # a server reachable from the Internet must not start that way by mistake (a missing or
        # incomplete .env file).
        if not self.is_production:
            public = [
                f"AGAURA_{name.upper()}={value}"
                for name, value, host in (
                    ("base_url", self.base_url, urlsplit(self.base_url).hostname or ""),
                    ("domain", self.domain, self.domain),
                )
                if not is_local_host(host)
            ]
            if public:
                msg = (
                    f"AGAURA_ENVIRONMENT=development is refused for a public address "
                    f"({', '.join(public)}): set AGAURA_ENVIRONMENT=production"
                )
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_email_from(self) -> Self:
        # Refused at start: a sender that cannot be a header would stop every email later.
        message = EmailMessage()
        try:
            message["From"] = self.email_from
            message.as_bytes()
        except (ValueError, TypeError) as error:
            msg = "AGAURA_EMAIL_FROM is not a valid sender (one line, an address)"
            raise ValueError(msg) from error
        if "@" not in parseaddr(self.email_from)[1]:
            msg = "AGAURA_EMAIL_FROM is not a valid sender (one line, an address)"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_production_on_a_public_address(self) -> Self:
        # The links of emails and the QR codes of printed code cards are built from base_url: a
        # forgotten https://localhost would ship them broken. The console backend sends nothing,
        # so invitations, password resets and D12 warnings would vanish (the account deleted 30
        # days after a warning never delivered). A production trial on this machine (Docker on
        # https://localhost) keeps both.
        if not self.is_production:
            return self
        parts = urlsplit(self.base_url)
        host = (parts.hostname or "").rstrip(".").lower()
        domain = self.domain.strip().rstrip(".").lower()
        if is_local_host(host) and is_local_host(domain):
            return self
        if parts.scheme != "https" or host != domain:
            msg = (
                f"AGAURA_BASE_URL={self.base_url} must be the HTTPS address of "
                f"AGAURA_DOMAIN={self.domain}: https://{domain}"
            )
            raise ValueError(msg)
        if self.email_backend is EmailBackend.CONSOLE:
            msg = "AGAURA_EMAIL_BACKEND=console sends no email: set smtp on a public address"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _require_real_secrets_in_production(self) -> Self:
        if self.is_production:
            key = self.secret_key.get_secret_value()
            if key == DEVELOPMENT_SECRET_KEY or len(key) < MIN_SECRET_KEY_LENGTH:
                msg = (
                    f"AGAURA_SECRET_KEY must be set to at least {MIN_SECRET_KEY_LENGTH} characters"
                )
                raise ValueError(msg)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
