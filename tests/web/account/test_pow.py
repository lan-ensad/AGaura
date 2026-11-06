"""Anti-bot proof of work (ALTCHA protocol): what ``pow.verify`` accepts and refuses."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from agaura.security import Keyring
from agaura.web import pow as proof_of_work
from tests.conftest import TEST_SECRET_KEY

NOW = 1_800_000_000.0


@pytest.fixture
def keyring() -> Keyring:
    return Keyring(TEST_SECRET_KEY)


@pytest.fixture
def challenge(keyring: Keyring) -> dict[str, Any]:
    return proof_of_work.create_challenge(keyring, now=NOW)


def encode(data: Any) -> str:
    return base64.b64encode(json.dumps(data).encode()).decode()


def solution(challenge: dict[str, Any], changes: dict[str, Any] | None = None) -> dict[str, Any]:
    number = proof_of_work.solve(challenge)
    assert number is not None
    payload = json.loads(base64.b64decode(proof_of_work.encode_solution(challenge, number)))
    return {**payload, **(changes or {})}


def verify(keyring: Keyring, payload: str, now: float = NOW + 1) -> bool:
    return proof_of_work.verify(keyring, payload, now=now, guard=proof_of_work.ReplayGuard())


def test_challenge_format(challenge: dict[str, Any], keyring: Keyring) -> None:
    assert challenge["algorithm"] == "SHA-256"
    assert challenge["maxnumber"] == proof_of_work.MAX_NUMBER
    expires = int(NOW + proof_of_work.CHALLENGE_LIFETIME)
    assert challenge["salt"].endswith(f"?expires={expires}&")
    assert challenge["signature"] == keyring.sign("altcha", challenge["challenge"])


def test_solved_challenge_is_accepted_once(challenge: dict[str, Any], keyring: Keyring) -> None:
    guard = proof_of_work.ReplayGuard()
    payload = encode(solution(challenge))
    assert proof_of_work.verify(keyring, payload, now=NOW + 1, guard=guard)
    assert not proof_of_work.verify(keyring, payload, now=NOW + 2, guard=guard)


def test_expired(challenge: dict[str, Any], keyring: Keyring) -> None:
    payload = encode(solution(challenge))
    assert not verify(keyring, payload, now=NOW + proof_of_work.CHALLENGE_LIFETIME)


@pytest.mark.parametrize(
    "changes",
    [
        {"algorithm": "SHA-1"},
        {"number": "12"},
        {"number": True},
        {"number": -1},
        {"number": proof_of_work.MAX_NUMBER + 1},
        {"signature": "0" * 64},
        {"signature": None},
        {"challenge": "0" * 64},
        {"salt": "short?expires=99999999999&"},
        {"salt": None},
    ],
)
def test_tampered_solutions(
    challenge: dict[str, Any], keyring: Keyring, changes: dict[str, Any]
) -> None:
    assert not verify(keyring, encode(solution(challenge, changes)))


def test_digits_cannot_move_from_the_number_into_the_salt(keyring: Keyring) -> None:
    """Moving digits into the salt keeps the hash but would push the expiry back."""
    for _ in range(50):
        challenge = proof_of_work.create_challenge(keyring, now=NOW)
        good = solution(challenge)
        digits = str(good["number"])
        if len(digits) > 1:
            break
    spliced = {**good, "salt": good["salt"] + digits[0], "number": int(digits[1:])}
    assert not verify(keyring, encode(spliced))
    assert verify(keyring, encode(good))


@pytest.mark.parametrize(
    "payload",
    ["", "%%%", base64.b64encode(b"not json").decode(), encode([1, 2]), "A" * 2000],
)
def test_malformed_payloads(keyring: Keyring, payload: str) -> None:
    assert not verify(keyring, payload)


def test_expiry_too_far_in_the_future_is_refused(keyring: Keyring) -> None:
    challenge = proof_of_work.create_challenge(keyring, now=NOW + 3600)
    assert not verify(keyring, encode(solution(challenge)))


def test_replay_guard_forgets_expired_challenges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proof_of_work.ReplayGuard, "MAX_ENTRIES", 2)
    guard = proof_of_work.ReplayGuard()
    assert guard.use("a", expires=10, now=0)
    assert guard.use("b", expires=100, now=0)
    assert guard.use("c", expires=100, now=50)  # full: "a" has expired and is dropped
    assert not guard.use("b", expires=100, now=50)
    assert guard.use("a", expires=100, now=50)


def test_solve_gives_up_on_impossible_challenges() -> None:
    impossible = {"salt": "x", "challenge": "0" * 64, "maxnumber": 10}
    assert proof_of_work.solve(impossible) is None
