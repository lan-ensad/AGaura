"""Secrets: random identifiers, token hashing, voter code digests, encryption, password hashing.

Nothing in this module logs or stores a plaintext secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

# Crockford base32: no I, L, O or U, so codes read aloud or typed stay unambiguous.
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
DIGITS = frozenset("0123456789")

PUBLIC_ID_LENGTH = 6
VOTER_CODE_LENGTH = 8
# 8 digits: about 100 million values, against guessing during the session (SPEC §9.3).
SESSION_PASSWORD_DIGITS = 8
MIN_SESSION_PASSWORD_LENGTH = 8

# FR-24: ignore case, spaces and hyphens (including typographic dashes inserted by phone
# keyboards), and map look-alikes onto the Crockford alphabet.
_DASHES = "-\u2010\u2011\u2012\u2013\u2014\u2212"
_NORMALIZE = str.maketrans({"O": "0", "I": "1", "L": "1", **dict.fromkeys(_DASHES, None)})


def normalize_secret(raw: str) -> str:
    """Normalize a session password, voter code or session ID typed by a human."""
    return "".join(raw.split()).upper().translate(_NORMALIZE)


def random_crockford(length: int) -> str:
    return "".join(secrets.choice(CROCKFORD_ALPHABET) for _ in range(length))


def new_public_id() -> str:
    """Session ID: 6 Crockford characters with at least one digit (never a fixed route word)."""
    while True:
        candidate = random_crockford(PUBLIC_ID_LENGTH)
        if DIGITS.intersection(candidate):
            return candidate


def new_voter_code() -> str:
    return random_crockford(VOTER_CODE_LENGTH)


def format_voter_code(code: str) -> str:
    """Display form of a voter code: ``K7M4-QX9P``."""
    half = len(code) // 2
    return f"{code[:half]}-{code[half:]}"


def new_session_password() -> str:
    return f"{secrets.randbelow(10**SESSION_PASSWORD_DIGITS):0{SESSION_PASSWORD_DIGITS}d}"


def new_token() -> str:
    """High-entropy random token for cookies and secret links."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Digest of a high-entropy token; a plain hash is enough at 256 bits of entropy."""
    return hashlib.sha256(token.encode()).hexdigest()


def secrets_match(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class Keyring:
    """Keys derived from the master secret, one per purpose."""

    def __init__(self, secret_key: str) -> None:
        self._secret = secret_key.encode()
        self._fernet = Fernet(base64.urlsafe_b64encode(self._derive("secret-encryption")))
        self._code_key = self._derive("voter-code")
        # HMAC key of the audit log chain (``agaura.services.audit``), handed to ``Database``.
        self.audit_key = self._derive("audit")

    def _derive(self, purpose: str) -> bytes:
        return hmac.new(self._secret, f"agaura:{purpose}".encode(), hashlib.sha256).digest()

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode()
        except InvalidToken as error:
            msg = "ciphertext cannot be decrypted with the current secret key"
            raise ValueError(msg) from error

    def sign(self, purpose: str, message: str) -> str:
        """HMAC-SHA256 of ``message`` with a key dedicated to ``purpose`` (hex)."""
        return hmac.new(
            self._derive(f"sign:{purpose}"), message.encode(), hashlib.sha256
        ).hexdigest()

    def code_digest(self, normalized_code: str) -> str:
        """HMAC of a voter code: 40-bit codes are too short for a plain hash (FR-23)."""
        return hmac.new(self._code_key, normalized_code.encode(), hashlib.sha256).hexdigest()


# argon2id with the library's defaults (RFC 9106 low-memory profile: 3 passes over 64 MiB,
# 4 lanes): about 45 ms and 64 MiB per hash or verification.
_password_hasher = PasswordHasher()

# Only account holders use passwords (voters join with a session password or a code, checked by
# HMAC), so a few hashes at a time are plenty. Unbounded, a burst of logins would hold 64 MiB
# each (16 of them fill the 1 GiB of the container, ``compose.yaml``) and every worker thread,
# which votes also need. Two at a time keep the memory under 130 MiB and the throughput near
# what two cores give; six more callers may wait, about 0.2 s, for their turn; past that, or
# after ``PASSWORD_HASHING_WAIT`` on a slow machine, ``PasswordBusyError`` (answered 503 with
# ``Retry-After``, ``agaura.main``), so that at most eight worker threads ever wait on argon2.
PASSWORD_HASHING_SLOTS = 2
PASSWORD_HASHING_QUEUE = 6
PASSWORD_HASHING_WAIT = 2.0  # seconds
PASSWORD_BUSY_RETRY_AFTER = 5  # seconds, suggested to the client of a 503


class PasswordBusyError(Exception):
    """Too many passwords are being hashed or verified at once."""


class WorkGate:
    """At most ``slots`` callers at work and ``queue`` more waiting, each for ``wait`` seconds
    at most; any other caller is refused at once with ``PasswordBusyError``.

    Re-entrant within a thread: a route reserves a slot (``password_slot``) before opening a
    write transaction, so that it never waits for its turn while holding the database's write
    lock, and the hashing inside uses the slot it already holds.
    """

    def __init__(self, slots: int, queue: int, wait: float) -> None:
        self._slots = threading.BoundedSemaphore(slots)
        self._callers = threading.BoundedSemaphore(slots + queue)
        self._wait = wait
        self._thread = threading.local()

    @contextmanager
    def slot(self) -> Iterator[None]:
        if getattr(self._thread, "holds_slot", False):
            yield
            return
        if not self._callers.acquire(blocking=False):
            raise PasswordBusyError
        try:
            if not self._slots.acquire(timeout=self._wait):
                raise PasswordBusyError
            self._thread.holds_slot = True
            try:
                yield
            finally:
                self._thread.holds_slot = False
                self._slots.release()
        finally:
            self._callers.release()


_password_gate = WorkGate(PASSWORD_HASHING_SLOTS, PASSWORD_HASHING_QUEUE, PASSWORD_HASHING_WAIT)


def password_slot() -> AbstractContextManager[None]:
    """Hold one of the ``PASSWORD_HASHING_SLOTS`` (re-entrant, see ``WorkGate``)."""
    return _password_gate.slot()


def hash_password(password: str) -> str:
    with password_slot():
        return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    with password_slot():
        try:
            return _password_hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False
