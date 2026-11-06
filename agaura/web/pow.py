"""Self-hosted anti-bot proof of work, following the ALTCHA protocol (FR-01, SPEC §9.3).

The server picks a secret number ``n`` up to ``MAX_NUMBER`` and hands out
``challenge = SHA-256(salt + str(n))``, signed with a key of its own. The browser
(``static/js/pow.js``) tries every number until the hash matches, then posts the base64 JSON
``{algorithm, challenge, number, salt, signature}`` in the ``altcha`` form field. Checking it
costs one hash and one HMAC; finding it costs the client about ``MAX_NUMBER / 2`` hashes.

The salt carries its own expiry (``<random>?expires=<unix time>&``). The trailing ``&`` and a
strict format stop anyone from moving digits of the number into the salt to push the expiry
back. Each solution is accepted once: used challenges are remembered in memory until they
expire (one process, like the rate limiter).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from typing import Any

from agaura.security import Keyring

ALGORITHM = "SHA-256"
MAX_NUMBER = 50_000  # difficulty: about 25,000 hashes on average, well under a second
CHALLENGE_LIFETIME = 30 * 60  # seconds; the page fetches a fresh challenge before that
PAGE_REFRESH = CHALLENGE_LIFETIME - 120  # seconds: a page solves a new challenge this often
SIGNING_PURPOSE = "altcha"
MAX_PAYLOAD_LENGTH = 1000
_SALT = re.compile(r"[0-9a-f]{24}\?expires=([0-9]{1,12})&")


def _hash(salt: str, number: int) -> str:
    return hashlib.sha256(f"{salt}{number}".encode()).hexdigest()


def create_challenge(keyring: Keyring, *, now: float | None = None) -> dict[str, Any]:
    """A new challenge, as sent to the browser."""
    expires = int((time.time() if now is None else now) + CHALLENGE_LIFETIME)
    salt = f"{secrets.token_hex(12)}?expires={expires}&"
    number = secrets.randbelow(MAX_NUMBER + 1)
    challenge = _hash(salt, number)
    return {
        "algorithm": ALGORITHM,
        "challenge": challenge,
        "maxnumber": MAX_NUMBER,
        "salt": salt,
        "signature": keyring.sign(SIGNING_PURPOSE, challenge),
    }


def solve(challenge: dict[str, Any]) -> int | None:
    """What the browser does: the number behind a challenge (tests, documentation)."""
    for number in range(int(challenge["maxnumber"]) + 1):
        if _hash(challenge["salt"], number) == challenge["challenge"]:
            return number
    return None


def encode_solution(challenge: dict[str, Any], number: int) -> str:
    """The ``altcha`` form field for a solved challenge."""
    payload = {
        "algorithm": challenge["algorithm"],
        "challenge": challenge["challenge"],
        "number": number,
        "salt": challenge["salt"],
        "signature": challenge["signature"],
    }
    return base64.b64encode(json.dumps(payload).encode()).decode("ascii")


class ReplayGuard:
    """Challenges already used, kept until they expire."""

    MAX_ENTRIES = 100_000

    def __init__(self) -> None:
        self._used: dict[str, float] = {}
        self._lock = threading.Lock()

    def use(self, challenge: str, expires: float, now: float) -> bool:
        """Mark a challenge as used; False if it already was."""
        with self._lock:
            if len(self._used) >= self.MAX_ENTRIES:
                self._used = {key: until for key, until in self._used.items() if until > now}
            if challenge in self._used:
                return False
            self._used[challenge] = expires
            return True


_guard = ReplayGuard()


def _decode(payload: str) -> dict[str, Any] | None:
    if not payload or len(payload) > MAX_PAYLOAD_LENGTH:
        return None
    try:
        data = json.loads(base64.b64decode(payload, validate=True))
    except (binascii.Error, ValueError):
        return None
    return data if isinstance(data, dict) else None


def verify(
    keyring: Keyring,
    payload: str,
    *,
    now: float | None = None,
    guard: ReplayGuard | None = None,
) -> bool:
    """Check a solution posted by a form: format, expiry, hash, signature, single use."""
    now = time.time() if now is None else now
    data = _decode(payload)
    if data is None:
        return False
    salt, challenge, number = data.get("salt"), data.get("challenge"), data.get("number")
    signature = data.get("signature")
    if (
        data.get("algorithm") != ALGORITHM
        or not isinstance(salt, str)
        or not isinstance(challenge, str)
        or not isinstance(signature, str)
        or not isinstance(number, int)
        or isinstance(number, bool)
        or not 0 <= number <= MAX_NUMBER
    ):
        return False
    match = _SALT.fullmatch(salt)
    if match is None:
        return False
    expires = int(match.group(1))
    if expires <= now or expires > now + CHALLENGE_LIFETIME + 60:
        return False
    if not hmac.compare_digest(_hash(salt, number), challenge):
        return False
    if not hmac.compare_digest(keyring.sign(SIGNING_PURPOSE, challenge), signature):
        return False
    return (guard or _guard).use(challenge, expires, now)
