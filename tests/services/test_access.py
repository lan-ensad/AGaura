import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text

from agaura.models import AccessMode, Assembly, User, VoterCredential
from agaura.security import normalize_secret
from agaura.services import access, assemblies, polls
from agaura.services.errors import (
    AccessDeniedError,
    InvalidInputError,
    InvalidStateError,
    NotFoundError,
    QuotaExceededError,
)
from tests.conftest import World


def attendees(world: World, owner: User, public_id: str) -> int:
    with world.database.read() as db:
        return access.attendees(db, assemblies.get_assembly(db, owner, public_id))


def credential(world: World, public_id: str, token: str) -> VoterCredential | None:
    with world.database.read() as db:
        return access.credential_for(db, public_id, token)


def join_error(world: World, public_id: str, secret: str) -> str:
    with pytest.raises(AccessDeniedError) as error:
        world.join(public_id, secret)
    return error.value.code


class TestPasswordMode:
    def test_join_with_a_forgiving_password(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        password = world.password(owner, public_id)
        token = world.join(public_id.lower(), f" {password[:3]} {password[3:]} ")
        assert credential(world, public_id, token) is not None
        assert attendees(world, owner, public_id) == 1

    def test_refusals(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False)
        password = world.password(owner, public_id)
        assert join_error(world, public_id, password) == "session_not_in_session"
        with world.database.write() as db:
            assemblies.open_assembly(db, owner, public_id)
        assert join_error(world, public_id, "000000x") == "wrong_secret"
        with pytest.raises(NotFoundError):
            world.join("ZZZZZ9", password)

    def test_lock_entries(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        password = world.password(owner, public_id)
        early = world.join(public_id, password)
        with world.database.write() as db:
            access.set_entries_locked(db, owner, public_id, True)
            access.set_entries_locked(db, owner, public_id, True)  # idempotent
        assert join_error(world, public_id, password) == "entries_locked"
        assert credential(world, public_id, early) is not None  # already joined: unaffected
        with world.database.write() as db:
            access.set_entries_locked(db, owner, public_id, False)
        world.join(public_id, password)
        assert attendees(world, owner, public_id) == 2

    def test_changing_the_password_keeps_voters_connected(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        token = world.voters(owner, public_id, 1)[0]
        old = world.password(owner, public_id)
        with world.database.write() as db:
            regenerated = access.set_session_password(db, world.keyring, owner, public_id)
            assert len(regenerated) == 8 and regenerated.isdigit()
            custom = access.set_session_password(
                db, world.keyring, owner, public_id, "  Assemblée  2026 "
            )
        assert custom == "Assemblée 2026"
        assert credential(world, public_id, token) is not None
        assert join_error(world, public_id, old) == "wrong_secret"
        world.join(public_id, "assemblée2026")

    @pytest.mark.parametrize(
        ("raw", "code"), [("abc1234", "password_too_short"), ("x" * 41, "password_too_long")]
    )
    def test_custom_password_length(self, world: World, owner: User, raw: str, code: str) -> None:
        public_id = world.assembly(owner)
        with pytest.raises(InvalidInputError, match=code), world.database.write() as db:
            access.set_session_password(db, world.keyring, owner, public_id, raw)

    def test_leave_ends_the_credential_on_this_device(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        token = world.voters(owner, public_id, 1)[0]
        with world.database.write() as db:
            access.leave(db, public_id, token)
            access.leave(db, public_id, token)  # already gone: no error
            access.leave(db, public_id, None)
            access.leave(db, "ZZZZZ9", token)
        assert credential(world, public_id, token) is None
        assert attendees(world, owner, public_id) == 1  # still someone who attended

    def test_credentials_stop_working_when_the_session_closes(
        self, world: World, owner: User
    ) -> None:
        public_id = world.assembly(owner)
        token = world.voters(owner, public_id, 1)[0]
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, public_id)
        assert credential(world, public_id, token) is None
        assert credential(world, public_id, "") is None
        assert credential(world, "ZZZZZ9", token) is None


class TestCodesMode:
    def test_codes_are_shown_once_and_stored_as_digests(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        codes = world.codes(owner, public_id, 20)
        assert len(set(codes)) == 20
        assert all(re.fullmatch(r"[0-9A-Z]{4}-[0-9A-Z]{4}", code) for code in codes)
        with world.database.read() as db:
            dump = "\n".join(db.connection().connection.iterdump())  # type: ignore[attr-defined]
        for code in codes:
            assert code not in dump and code.replace("-", "") not in dump
        assert attendees(world, owner, public_id) == 0  # not activated yet

    def test_credentials_are_not_stored_in_card_order(self, world: World, owner: User) -> None:
        """Credential ids, and the physical order of rows, must not tell which card is which."""
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        codes = world.codes(owner, public_id, 50)  # in the order of the cards of the PDF
        with world.database.read() as db:
            stored = list(
                db.scalars(select(VoterCredential.code_hmac).order_by(VoterCredential.id))
            )
        printed = [world.keyring.code_digest(normalize_secret(code)) for code in codes]
        assert sorted(stored) == sorted(printed)
        assert stored != printed  # 1 chance in 50! of a false failure

    def test_join_with_a_code(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        code = world.codes(owner, public_id, 3)[0]
        token = world.join(public_id, code.lower().replace("-", " "))
        assert credential(world, public_id, token) is not None
        assert attendees(world, owner, public_id) == 1
        assert join_error(world, public_id, "K7M4-QX9P") == "wrong_secret"
        assert join_error(world, public_id, "too short") == "wrong_secret"

    def test_password_does_not_open_a_codes_session(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        assert join_error(world, public_id, world.password(owner, public_id)) == "wrong_secret"

    def test_code_entered_again_moves_to_the_new_device(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        code = world.codes(owner, public_id, 2)[0]
        first = world.join(public_id, code)
        with world.database.write() as db:
            access.set_entries_locked(db, owner, public_id, True)
        second = world.join(public_id, code)  # allowed while locked: not a new activation
        assert credential(world, public_id, first) is None
        assert credential(world, public_id, second) is not None
        assert attendees(world, owner, public_id) == 1

    def test_lock_refuses_new_activations(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        codes = world.codes(owner, public_id, 2)
        with world.database.write() as db:
            access.set_entries_locked(db, owner, public_id, True)
        assert join_error(world, public_id, codes[1]) == "entries_locked"

    def test_revoked_batch(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        codes = world.codes(owner, public_id, 2)
        token = world.join(public_id, codes[0])
        with world.database.write() as db:
            batch = access.list_code_batches(db, owner, public_id)[0]
            access.revoke_code_batch(db, owner, public_id, batch.id)
            access.revoke_code_batch(db, owner, public_id, batch.id)  # idempotent
            with pytest.raises(NotFoundError):
                access.revoke_code_batch(db, owner, public_id, batch.id + 1)
        assert credential(world, public_id, token) is None
        assert join_error(world, public_id, codes[1]) == "wrong_secret"
        assert attendees(world, owner, public_id) == 0
        world.codes(owner, public_id, 1)  # a new batch can replace it

    @pytest.mark.parametrize("size", [0, 2001])
    def test_batch_size(self, world: World, owner: User, size: int) -> None:
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        with pytest.raises(InvalidInputError, match="invalid_batch_size"):
            world.codes(owner, public_id, size)

    def test_codes_quota(self, world: World) -> None:
        owner = world.user(codes_per_assembly=5)
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        world.codes(owner, public_id, 4)
        with pytest.raises(QuotaExceededError, match="codes_per_assembly"):
            world.codes(owner, public_id, 2)

    def test_revoked_codes_count_toward_the_quota(self, world: World) -> None:
        """Generating and revoking in a loop stays bounded (SPEC FR-08)."""
        owner = world.user(codes_per_assembly=5)
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        world.codes(owner, public_id, 3)
        with world.database.write() as db:
            batch = access.list_code_batches(db, owner, public_id)[0]
            access.revoke_code_batch(db, owner, public_id, batch.id)
        world.codes(owner, public_id, 2)  # a lost batch can be replaced, up to the quota
        with pytest.raises(QuotaExceededError, match="codes_per_assembly"):
            world.codes(owner, public_id, 1)

    def test_no_codes_once_closed(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, public_id)
        with pytest.raises(InvalidStateError):
            world.codes(owner, public_id, 1)


class TestAccessCap:
    """Password mode issues at most the registered members plus a margin of credentials (FR-27):
    a script that drops its cookie cannot inflate the attendees nor stuff ballots."""

    @pytest.mark.parametrize(
        ("registered", "cap"), [(1, 11), (100, 110), (101, 112), (250, 275), (None, 2000)]
    )
    def test_cap(self, world: World, owner: User, registered: int | None, cap: int) -> None:
        public_id = world.assembly(owner, registered_count=registered)
        with world.database.read() as db:
            assert access.access_cap(assemblies.get_assembly(db, owner, public_id)) == cap

    def test_joins_stop_at_the_cap(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, registered_count=1)  # cap: 1 + 10
        password = world.password(owner, public_id)
        tokens = world.voters(owner, public_id, 11)
        assert join_error(world, public_id, password) == access.ACCESS_CAP_REACHED
        assert join_error(world, public_id, "00000000") == "wrong_secret"  # nothing told
        assert attendees(world, owner, public_id) == 11
        # Leaving frees no place: the device may have voted already.
        with world.database.write() as db:
            access.leave(db, public_id, tokens[0])
        assert join_error(world, public_id, password) == access.ACCESS_CAP_REACHED
        assert all(credential(world, public_id, token) for token in tokens[1:])  # unaffected

    def test_the_headcount_raises_the_cap(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, registered_count=1)
        world.voters(owner, public_id, 11)
        with world.database.write() as db:
            access.set_headcount(db, owner, public_id, 0)  # never lowers it
        assert join_error(world, public_id, world.password(owner, public_id)) == (
            access.ACCESS_CAP_REACHED
        )
        with world.database.write() as db:
            access.set_headcount(db, owner, public_id, 20)  # cap: 20 + 10
        world.voters(owner, public_id, 19)
        assert join_error(world, public_id, world.password(owner, public_id)) == (
            access.ACCESS_CAP_REACHED
        )

    def test_the_headcount_stays_within_the_codes_quota(self, world: World) -> None:
        """A delegate link holding the headcount right only must not lift the cap without
        bound: past the codes quota, the cap of a session without a member count, refused."""
        owner = world.user(codes_per_assembly=50)
        public_id = world.assembly(owner, registered_count=1)
        with world.database.write() as db:
            access.set_headcount(db, owner, public_id, 50)
        with world.database.write() as db, pytest.raises(InvalidInputError) as refused:
            access.set_headcount(db, owner, public_id, 51)
        assert refused.value.code == "headcount_too_large"
        with world.database.read() as db:
            assembly = db.scalars(select(Assembly).where(Assembly.public_id == public_id)).one()
            assert assembly.headcount == 50
            assert access.access_cap(assembly) == 60

    def test_default_cap_is_the_codes_quota(self, world: World) -> None:
        owner = world.user(codes_per_assembly=3)
        public_id = world.assembly(owner)  # no registered members
        world.voters(owner, public_id, 3)
        assert join_error(world, public_id, world.password(owner, public_id)) == (
            access.ACCESS_CAP_REACHED
        )
        with world.database.read() as db:
            assembly = assemblies.get_assembly(db, owner, public_id)
            assert access.access_cap_reached(assembly, access.attendees(db, assembly)) == 3

    def test_codes_mode_is_not_capped(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, mode=AccessMode.CODES, registered_count=1)
        for code in world.codes(owner, public_id, 12):
            world.join(public_id, code)
        assert attendees(world, owner, public_id) == 12
        with world.database.read() as db:
            assembly = assemblies.get_assembly(db, owner, public_id)
            assert access.access_cap_reached(assembly, 12) is None

    def test_concurrent_joins_never_overshoot(self, world: World, owner: User) -> None:
        """Each join counts and inserts in its own BEGIN IMMEDIATE transaction."""
        public_id = world.assembly(owner, registered_count=1)
        password = world.password(owner, public_id)

        def attempt(_: int) -> str:
            try:
                world.join(public_id, password)
            except AccessDeniedError as error:
                return error.code
            return "joined"

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(24)))
        assert outcomes.count("joined") == 11
        assert outcomes.count(access.ACCESS_CAP_REACHED) == 13
        assert attendees(world, owner, public_id) == 11


def test_switching_to_codes_mode_disables_password_devices(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    device = world.voters(owner, public_id, 2)[0]
    codes = world.codes(owner, public_id, 3)
    with world.database.write() as db:
        access.set_access_mode(db, owner, public_id, AccessMode.CODES)
        access.set_access_mode(db, owner, public_id, AccessMode.CODES)  # no change
    assert credential(world, public_id, device) is None
    assert attendees(world, owner, public_id) == 0
    world.join(public_id, codes[0])
    assert attendees(world, owner, public_id) == 1


class TestNoNewCredentialsDuringAVote:
    """While a poll accepts ballots, nothing may hand a person who already voted on it a second
    credential: no access mode switch, no new code batch."""

    def test_mode_switch_waits_for_the_poll_to_close(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        device = world.voters(owner, public_id, 1)[0]
        poll_id = world.poll(owner, public_id)
        world.vote(public_id, device, poll_id, {"choice": 1})
        with (
            pytest.raises(InvalidStateError, match="access_mode_poll_open"),
            world.database.write() as db,
        ):
            access.set_access_mode(db, owner, public_id, AccessMode.CODES)
        with world.database.write() as db:
            access.set_access_mode(db, owner, public_id, AccessMode.PASSWORD)  # no change: fine
            polls.close_poll(db, owner, poll_id)
            access.set_access_mode(db, owner, public_id, AccessMode.CODES)
        assert credential(world, public_id, device) is None

    def test_codes_wait_for_the_poll_to_close(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        world.codes(owner, public_id, 2)
        poll_id = world.poll(owner, public_id)
        with pytest.raises(InvalidStateError, match="codes_poll_open"):
            world.codes(owner, public_id, 2)
        with world.database.write() as db:
            polls.close_poll(db, owner, poll_id)
        assert len(world.codes(owner, public_id, 2)) == 2

    def test_a_replaced_batch_cannot_vote_twice(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, mode=AccessMode.CODES)
        code = world.codes(owner, public_id, 1)[0]
        token = world.join(public_id, code)
        poll_id = world.poll(owner, public_id)
        world.vote(public_id, token, poll_id, {"choice": 1})
        with world.database.write() as db:
            batch = access.list_code_batches(db, owner, public_id)[0]
            access.revoke_code_batch(db, owner, public_id, batch.id)  # still possible: lost PDF
        with pytest.raises(InvalidStateError, match="codes_poll_open"):
            world.codes(owner, public_id, 1)


def test_headcount_warnings(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    tokens = world.voters(owner, public_id, 4)
    poll_id = world.poll(owner, public_id)
    for token in tokens:
        world.vote(public_id, token, poll_id, {"choice": 1})
    with world.database.write() as db:
        assembly = assemblies.get_assembly(db, owner, public_id)
        assert access.headcount_warnings(db, assembly) == []
        access.set_headcount(db, owner, public_id, 4)
        assert access.headcount_warnings(db, assembly) == []
        access.set_headcount(db, owner, public_id, 3)
        warnings = access.headcount_warnings(db, assembly)
        with pytest.raises(InvalidInputError):
            access.set_headcount(db, owner, public_id, -1)
    assert [(w.kind, w.count, w.headcount, w.poll_id) for w in warnings] == [
        ("attendees", 4, 3, None),
        ("ballots", 4, 3, poll_id),
    ]


class TestLinks:
    def test_screen_link(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        with world.database.write() as db:
            token = access.screen_token(db, world.keyring, owner, public_id)
            assert (
                access.check_link_token(db, world.keyring, public_id, token).public_id == public_id
            )
            new = access.regenerate_screen_token(db, world.keyring, owner, public_id)
            with pytest.raises(NotFoundError):
                access.check_link_token(db, world.keyring, public_id, token)
            access.check_link_token(db, world.keyring, public_id, new)
            with pytest.raises(NotFoundError):
                access.check_link_token(db, world.keyring, "ZZZZZ9", new)

    def test_public_results_link(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        with world.database.write() as db:
            assert access.public_results_token(db, world.keyring, owner, public_id) is None
            with pytest.raises(NotFoundError):
                access.check_link_token(db, world.keyring, public_id, "x", public=True)
            token = access.set_public_results(db, world.keyring, owner, public_id, True)
            assert token == access.public_results_token(db, world.keyring, owner, public_id)
            assembly = access.check_link_token(db, world.keyring, public_id, token, public=True)
            assert assembly.public_id == public_id
            assert access.set_public_results(db, world.keyring, owner, public_id, False) is None
            with pytest.raises(NotFoundError):
                access.check_link_token(db, world.keyring, public_id, token, public=True)


def test_other_accounts_cannot_touch_access(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    intruder = world.user("intruder@example.org")
    with world.database.write() as db:
        for action in (
            lambda: access.session_password(db, world.keyring, intruder, public_id),
            lambda: access.set_entries_locked(db, intruder, public_id, True),
            lambda: access.create_code_batch(db, world.keyring, intruder, public_id, 1),
            lambda: access.screen_token(db, world.keyring, intruder, public_id),
            lambda: polls.list_polls(db, intruder, public_id),
        ):
            with pytest.raises(NotFoundError):
                action()


def test_credentials_carry_no_secret_in_clear(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    token = world.voters(owner, public_id, 1)[0]
    with world.database.read() as db:
        stored = db.scalar(select(VoterCredential.token_hash))
        raw = db.scalar(text("SELECT group_concat(token_hash) FROM voter_credential"))
    assert stored is not None and token not in raw


def test_code_collisions_are_skipped(
    world: World, owner: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    drawn = iter(["AAAAAAAA", "AAAAAAAA", "BBBBBBBB"])
    monkeypatch.setattr(access, "new_voter_code", lambda: next(drawn))
    assert world.codes(owner, public_id, 2) == ["AAAA-AAAA", "BBBB-BBBB"]
