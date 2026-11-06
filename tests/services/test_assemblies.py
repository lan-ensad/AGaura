import datetime as dt

import pytest
from sqlalchemy import func, select, text

from agaura.models import (
    Assembly,
    AssemblyStatus,
    AuditEntry,
    Ballot,
    Poll,
    User,
    VoterCredential,
)
from agaura.services import assemblies
from agaura.services.assemblies import AssemblyInput
from agaura.services.errors import (
    InvalidInputError,
    InvalidStateError,
    NotFoundError,
    QuotaExceededError,
)
from tests.conftest import World


def test_creation(world: World, owner: User) -> None:
    public_id = world.assembly(
        owner,
        in_session=False,
        title="  AG 2026 ",
        date=dt.date(2026, 6, 20),
        place="Salle des fêtes",
        registered_count=120,
        quorum_rule=" 1 / 4 ",
    )
    with world.database.read() as db:
        assembly = assemblies.get_assembly(db, owner, public_id)
        raw_password_column = db.scalar(
            text("SELECT access_password_enc FROM assembly WHERE id = :id"), {"id": assembly.id}
        )
    assert assembly.status is AssemblyStatus.PREPARATION
    assert (assembly.title, assembly.quorum_rule) == ("AG 2026", "1/4")
    assert assemblies.quorum_required(assembly) == 30
    password = world.password(owner, public_id)
    assert len(password) == 8 and password.isdigit()
    assert password not in raw_password_column  # stored encrypted (FR-20)


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"title": "  "}, "required"),
        ({"title": "x" * 201}, "too_long"),
        ({"place": "x" * 201}, "too_long"),
        ({"title": "AG\x1b[8m"}, "control_characters"),
        ({"place": "Salle\x00B"}, "control_characters"),
        ({"registered_count": 0}, "invalid_registered_count"),
        ({"quorum_rule": "0"}, "invalid_quorum_rule"),
        ({"quorum_rule": "3/2"}, "invalid_quorum_rule"),
        ({"quorum_rule": "1/0"}, "invalid_quorum_rule"),
        ({"quorum_rule": "half"}, "invalid_quorum_rule"),
        ({"quorum_rule": "1" * 21}, "invalid_quorum_rule"),
    ],
)
def test_invalid_details(world: World, owner: User, fields: dict[str, object], code: str) -> None:
    fields.setdefault("title", "AG")
    with pytest.raises(InvalidInputError) as error, world.database.write() as db:
        assemblies.create_assembly(db, world.keyring, owner, AssemblyInput(**fields))  # type: ignore[arg-type]
    assert error.value.code == code


@pytest.mark.parametrize(
    ("rule", "registered", "required"),
    [(None, 100, None), ("50", None, 50), ("1/3", 100, 34), ("1/3", None, None)],
)
def test_quorum_required(rule: str | None, registered: int | None, required: int | None) -> None:
    assembly = Assembly(quorum_rule=rule, registered_count=registered)
    assert assemblies.quorum_required(assembly) == required


def test_session_ids_are_forgiving(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    typed = public_id.lower().replace("0", "o").replace("1", "l")
    with world.database.read() as db:
        assert assemblies.get_assembly(db, owner, typed).public_id == public_id
        assert assemblies.find_assembly(db, typed) is not None
        assert assemblies.find_assembly(db, "login") is None
    assert assemblies.normalize_public_id("abc") is None


class TestIsolation:
    def test_other_accounts_see_nothing(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        intruder = world.user("intruder@example.org")
        with world.database.write() as db:
            assert assemblies.list_assemblies(db, intruder) == []
            for action in (
                lambda: assemblies.get_assembly(db, intruder, public_id),
                lambda: assemblies.close_assembly(db, intruder, public_id),
                lambda: assemblies.delete_assembly(db, intruder, public_id),
            ):
                with pytest.raises(NotFoundError):
                    action()

    def test_dashboard_lists_most_recent_first(self, world: World, owner: User) -> None:
        first = world.assembly(owner, title="Première")
        second = world.assembly(owner, title="Seconde")
        with world.database.read() as db:
            listed = [a.public_id for a in assemblies.list_assemblies(db, owner)]
        assert listed == [second, first]


class TestLifecycle:
    def test_full_lifecycle(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False)
        with world.database.write() as db:
            assemblies.update_assembly(db, owner, public_id, AssemblyInput(title="Nouveau titre"))
            assert assemblies.count_in_session(db) == 0
            opened = assemblies.open_assembly(db, owner, public_id)
            assert opened.status is AssemblyStatus.IN_SESSION and opened.opened_at is not None
            assert assemblies.count_in_session(db) == 1
            closed = assemblies.close_assembly(db, owner, public_id)
            assert closed.status is AssemblyStatus.CLOSED and closed.closed_at is not None

    @pytest.mark.parametrize(
        ("in_session", "action"),
        [
            (True, "open"),
            (True, "update"),
            (True, "delete"),
            (False, "close"),
        ],
    )
    def test_forbidden_transitions(
        self, world: World, owner: User, in_session: bool, action: str
    ) -> None:
        public_id = world.assembly(owner, in_session=in_session)
        calls = {
            "open": lambda db: assemblies.open_assembly(db, owner, public_id),
            "close": lambda db: assemblies.close_assembly(db, owner, public_id),
            "delete": lambda db: assemblies.delete_assembly(db, owner, public_id),
            "update": lambda db: assemblies.update_assembly(
                db, owner, public_id, AssemblyInput(title="x")
            ),
        }
        with pytest.raises(InvalidStateError), world.database.write() as db:
            calls[action](db)

    def test_closed_sessions_cannot_reopen(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, public_id)
        with (
            pytest.raises(InvalidStateError, match="assembly_closed"),
            world.database.write() as db,
        ):
            assemblies.open_assembly(db, owner, public_id)

    def test_cannot_close_with_an_open_poll(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        world.poll(owner, public_id)
        with (
            pytest.raises(InvalidStateError, match="polls_still_open"),
            world.database.write() as db,
        ):
            assemblies.close_assembly(db, owner, public_id)

    def test_delete_removes_everything_but_the_audit_log(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id)
        world.vote(public_id, world.voters(owner, public_id, 1)[0], poll_id, {"choice": 1})
        with world.database.write() as db:
            from agaura.services import polls

            polls.close_poll(db, owner, poll_id)
            assemblies.close_assembly(db, owner, public_id)
            assemblies.delete_assembly(db, owner, public_id)
        with world.database.read() as db:
            for model in (Assembly, Poll, Ballot, VoterCredential):
                assert db.scalar(select(func.count()).select_from(model)) == 0
            actions = list(db.scalars(select(AuditEntry.action)))
        assert actions[-1] == "assembly.deleted"


def test_quota_of_sessions_not_yet_closed(world: World) -> None:
    owner = world.user(open_assemblies=1)
    public_id = world.assembly(owner)
    with pytest.raises(QuotaExceededError, match="open_assemblies"):
        world.assembly(owner)
    with world.database.write() as db:
        assemblies.close_assembly(db, owner, public_id)
    world.assembly(owner)  # a closed session no longer counts


def test_session_id_collisions_are_skipped(
    world: World, owner: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = world.assembly(owner)
    drawn = iter([taken, "NEW123"])
    monkeypatch.setattr(assemblies, "new_public_id", lambda: next(drawn))
    assert world.assembly(owner) == "NEW123"
