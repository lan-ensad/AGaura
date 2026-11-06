import threading
import time

import pytest

from agaura import security
from agaura.config import Settings
from agaura.security import (
    CROCKFORD_ALPHABET,
    PASSWORD_HASHING_SLOTS,
    Keyring,
    PasswordBusyError,
    WorkGate,
    format_voter_code,
    hash_password,
    hash_token,
    new_public_id,
    new_session_password,
    new_token,
    new_voter_code,
    normalize_secret,
    password_slot,
    secrets_match,
    verify_password,
)


@pytest.mark.parametrize(
    ("typed", "normalized"),
    [
        ("k7m4-qx9p", "K7M4QX9P"),
        (" 123 456 ", "123456"),
        ("123\u00a0456", "123456"),  # non-breaking space
        ("K7M4\u2013QX9P", "K7M4QX9P"),  # en dash from a phone keyboard
        ("oil", "011"),  # look-alikes (FR-24)
    ],
)
def test_normalize_secret(typed: str, normalized: str) -> None:
    assert normalize_secret(typed) == normalized


def test_public_ids_always_contain_a_digit() -> None:
    for _ in range(300):
        public_id = new_public_id()
        assert len(public_id) == 6
        assert set(public_id) <= set(CROCKFORD_ALPHABET)
        assert any(char.isdigit() for char in public_id)


def test_voter_codes_and_session_passwords() -> None:
    code = new_voter_code()
    assert len(code) == 8 and set(code) <= set(CROCKFORD_ALPHABET)
    assert format_voter_code("K7M4QX9P") == "K7M4-QX9P"
    password = new_session_password()
    assert len(password) == 8 and password.isdigit()


def test_tokens_are_random_and_hashed() -> None:
    first, second = new_token(), new_token()
    assert first != second and len(first) >= 43
    assert len(hash_token(first)) == 64 and hash_token(first) != first
    assert secrets_match("abc", "abc") and not secrets_match("abc", "abd")


class TestKeyring:
    def test_encryption_round_trip(self) -> None:
        keyring = Keyring("first-secret-key")
        ciphertext = keyring.encrypt("204971")
        assert "204971" not in ciphertext
        assert keyring.decrypt(ciphertext) == "204971"

    def test_another_key_cannot_decrypt(self) -> None:
        ciphertext = Keyring("first-secret-key").encrypt("204971")
        with pytest.raises(ValueError, match="cannot be decrypted"):
            Keyring("second-secret-key").decrypt(ciphertext)

    def test_code_digests_depend_on_the_key(self) -> None:
        first, second = Keyring("first-secret-key"), Keyring("second-secret-key")
        assert first.code_digest("K7M4QX9P") == first.code_digest("K7M4QX9P")
        assert first.code_digest("K7M4QX9P") != second.code_digest("K7M4QX9P")

    def test_audit_key_is_its_own_and_follows_the_secret(self) -> None:
        first, second = Keyring("first-secret-key"), Keyring("second-secret-key")
        assert len(first.audit_key) == 32
        assert first.audit_key == Keyring("first-secret-key").audit_key != second.audit_key
        assert first.audit_key not in {first._code_key, first._derive("sign:audit")}
        settings = Settings(secret_key="first-secret-key", _env_file=None)  # type: ignore[call-arg]
        assert settings.audit_key == first.audit_key


def test_password_hashing() -> None:
    password_hash = hash_password("correct horse battery")
    assert password_hash.startswith("$argon2id$")
    assert verify_password(password_hash, "correct horse battery")
    assert not verify_password(password_hash, "wrong password!")
    assert not verify_password("not-a-hash", "correct horse battery")


class TestPasswordHashingGate:
    """Argon2 takes 64 MiB per hash: a burst of logins must not run them all at once."""

    @pytest.fixture
    def tracked(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
        """Replace argon2 with a slow stand-in that records how many run at once."""
        state = {"running": 0, "peak": 0}
        lock = threading.Lock()

        class SlowHasher:
            def _work(self) -> None:
                with lock:
                    state["running"] += 1
                    state["peak"] = max(state["peak"], state["running"])
                time.sleep(0.02)
                with lock:
                    state["running"] -= 1

            def hash(self, password: str) -> str:
                self._work()
                return "hash"

            def verify(self, password_hash: str, password: str) -> bool:
                self._work()
                return True

        monkeypatch.setattr(security, "_password_hasher", SlowHasher())
        return state

    def run_together(self, count: int) -> list[str]:
        barrier = threading.Barrier(count)
        outcomes: list[str] = []

        def run(index: int) -> None:
            barrier.wait()
            try:
                if index % 2:
                    hash_password("correct horse battery")
                else:
                    verify_password("hash", "correct horse battery")
                outcomes.append("done")
            except PasswordBusyError:
                outcomes.append("busy")

        threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return outcomes

    def test_never_more_than_the_slots_at_once(self, tracked: dict[str, int]) -> None:
        outcomes = self.run_together(40)
        assert tracked["peak"] <= PASSWORD_HASHING_SLOTS
        # The slots and the queue get through; the others are refused at once.
        assert outcomes.count("done") >= PASSWORD_HASHING_SLOTS
        assert outcomes.count("done") + outcomes.count("busy") == 40

    def test_waiting_callers_all_get_their_turn(
        self, tracked: dict[str, int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(security, "_password_gate", WorkGate(2, 30, 10.0))
        assert self.run_together(32) == ["done"] * 32
        assert tracked["peak"] == 2

    def test_full_queue_or_long_wait_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gate = WorkGate(1, 1, 0.05)
        monkeypatch.setattr(security, "_password_gate", gate)
        held = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with gate.slot():
                held.set()
                release.wait()

        holder = threading.Thread(target=hold)
        holder.start()
        held.wait()
        try:
            with pytest.raises(PasswordBusyError):  # waited 0.05 s in the queue
                hash_password("correct horse battery")
            waiting = threading.Thread(target=lambda: gate._callers.acquire())
            waiting.start()  # takes the only place in the queue
            waiting.join()
            started = time.monotonic()
            with pytest.raises(PasswordBusyError):  # no place left: refused at once
                hash_password("correct horse battery")
            assert time.monotonic() - started < 0.05
            gate._callers.release()
        finally:
            release.set()
            holder.join()
        assert verify_password(hash_password("correct horse battery"), "correct horse battery")

    def test_a_held_slot_is_reused_by_the_same_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A route takes its turn before the write transaction; hashing inside reuses it."""
        monkeypatch.setattr(security, "_password_gate", WorkGate(1, 0, 0.01))
        with password_slot():
            password_hash = hash_password("correct horse battery")
            assert verify_password(password_hash, "correct horse battery")
