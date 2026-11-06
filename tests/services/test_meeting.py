"""Tribune and agenda of a session: validation, owner isolation, audit, item under discussion."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from agaura.models import Assembly, AuditEntry, User
from agaura.services import assemblies, meeting
from agaura.services.errors import InvalidInputError, InvalidStateError, NotFoundError
from agaura.services.meeting import Member
from tests.conftest import World


def tribune_of(world: World, owner: User, public_id: str) -> list[dict[str, str]]:
    with world.database.read() as db:
        return assemblies.get_assembly(db, owner, public_id).tribune


def agenda_of(world: World, owner: User, public_id: str) -> tuple[list[str], int | None]:
    with world.database.read() as db:
        assembly = assemblies.get_assembly(db, owner, public_id)
        return meeting.agenda_titles(assembly), assembly.agenda_current


def entries(world: World, action: str) -> list[dict]:
    with world.database.read() as db:
        rows = db.scalars(select(AuditEntry).where(AuditEntry.action == action)).all()
        return [entry.details for entry in rows]


def set_tribune(world: World, owner: User, public_id: str, members: list[Member]) -> None:
    with world.database.write() as db:
        meeting.set_tribune(db, owner, public_id, members)


def set_agenda(world: World, owner: User, public_id: str, titles: list[str]) -> None:
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, titles)


def set_current(world: World, owner: User, public_id: str, position: int | None) -> None:
    with world.database.write() as db:
        meeting.set_current_item(db, owner, public_id, position)


def move(world: World, owner: User, public_id: str, step: int, seen: int | None) -> None:
    with world.database.write() as db:
        meeting.move_current_item(db, owner, public_id, step, seen=seen)


# --- Tribune ------------------------------------------------------------------------------------


def test_tribune_is_trimmed_in_order_and_empty_rows_are_ignored(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    members = [
        Member("  Marie Dupont ", " Présidente de séance "),
        Member("", ""),
        Member("   ", "\t"),
        Member("Paul Martin", "Secrétaire"),
        Member("Léa Bernard"),
    ]
    set_tribune(world, owner, public_id, members)
    assert tribune_of(world, owner, public_id) == [
        {"name": "Marie Dupont", "role": "Présidente de séance"},
        {"name": "Paul Martin", "role": "Secrétaire"},
        {"name": "Léa Bernard", "role": ""},
    ]
    set_tribune(world, owner, public_id, [])  # emptied
    assert tribune_of(world, owner, public_id) == []


@pytest.mark.parametrize(
    ("members", "code", "detail"),
    [
        ([Member("", "Secrétaire")], "meeting_name_required", "member:1"),
        ([Member("A"), Member("", ""), Member("x" * 101)], "meeting_name_too_long", "member:2"),
        ([Member("A", "x" * 101)], "meeting_role_too_long", "member:1"),
        ([Member(f"P{n}") for n in range(21)], "meeting_tribune_too_large", None),
    ],
)
def test_tribune_limits(
    world: World, owner: User, members: list[Member], code: str, detail: str | None
) -> None:
    public_id = world.assembly(owner)
    set_tribune(world, owner, public_id, [Member("Marie Dupont")])
    with pytest.raises(InvalidInputError) as raised, world.database.write() as db:
        meeting.set_tribune(db, owner, public_id, members)
    assert (raised.value.code, raised.value.detail) == (code, detail)
    assert tribune_of(world, owner, public_id) == [{"name": "Marie Dupont", "role": ""}]


def test_tribune_bounds_are_inclusive(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    members = [Member("n" * 100, "f" * 100)] + [Member(f"P{n}") for n in range(19)]
    set_tribune(world, owner, public_id, members)
    assert len(tribune_of(world, owner, public_id)) == 20


def test_every_tribune_problem_is_listed(world: World, owner: User) -> None:
    problems = meeting.tribune_problems(
        [Member(f"P{n}") for n in range(20)] + [Member("", "Scrutatrice"), Member("y", "z" * 101)]
    )
    assert [(problem.code, problem.detail) for problem in problems] == [
        ("meeting_tribune_too_large", None),
        ("meeting_name_required", "member:21"),
        ("meeting_role_too_long", "member:22"),
    ]


def test_tribune_is_editable_in_every_state_and_audited_without_names(
    world: World, owner: User
) -> None:
    public_id = world.assembly(owner, in_session=False)
    set_tribune(world, owner, public_id, [Member("Marie Dupont", "Présidente")])
    with world.database.write() as db:
        assemblies.open_assembly(db, owner, public_id)
        assemblies.close_assembly(db, owner, public_id)
    # A name fixed for the minutes after closing.
    set_tribune(world, owner, public_id, [Member("Marie Dupond", "Présidente"), Member("Paul")])
    assert tribune_of(world, owner, public_id)[0]["name"] == "Marie Dupond"
    assert entries(world, "meeting.tribune_set") == [{"count": 1}, {"count": 2}]
    with world.database.read() as db:
        texts = [str(entry.details) for entry in db.scalars(select(AuditEntry))]
    assert not any("Dupon" in text or "Présidente" in text or "Paul" in text for text in texts)


def test_another_account_gets_not_found(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    intruder = world.user("intruder@example.org")
    calls = [
        lambda db: meeting.set_tribune(db, intruder, public_id, [Member("X")]),
        lambda db: meeting.set_agenda(db, intruder, public_id, ["X"]),
        lambda db: meeting.set_current_item(db, intruder, public_id, None),
        lambda db: meeting.move_current_item(db, intruder, public_id, 1, seen=None),
    ]
    for call in calls:
        with pytest.raises(NotFoundError), world.database.write() as db:
            call(db)
    assert tribune_of(world, owner, public_id) == []
    assert entries(world, "meeting.tribune_set") == entries(world, "meeting.agenda_set") == []


# --- Agenda -------------------------------------------------------------------------------------


def test_agenda_blank_lines_ignored_and_audited(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    set_agenda(world, owner, public_id, ["  Rapport moral ", "", "   ", "Rapport financier"])
    assert agenda_of(world, owner, public_id) == (["Rapport moral", "Rapport financier"], None)
    with world.database.read() as db:
        stored = assemblies.get_assembly(db, owner, public_id).agenda
    assert stored == [{"title": "Rapport moral"}, {"title": "Rapport financier"}]
    assert entries(world, "meeting.agenda_set") == [{"count": 2}]


def test_agenda_saved_again_unchanged_logs_nothing(world: World, owner: User) -> None:
    """The card is edited in place: its button may be pressed with nothing new to save."""
    public_id = world.assembly(owner)
    set_agenda(world, owner, public_id, ["Rapport moral", "Rapport financier"])
    set_current(world, owner, public_id, 2)
    set_agenda(world, owner, public_id, [" Rapport moral", "", "Rapport financier "])
    assert agenda_of(world, owner, public_id) == (["Rapport moral", "Rapport financier"], 2)
    assert entries(world, "meeting.agenda_set") == [{"count": 2}]
    set_agenda(world, owner, public_id, ["Rapport financier", "Rapport moral"])  # reordered
    assert agenda_of(world, owner, public_id) == (["Rapport financier", "Rapport moral"], 1)
    assert entries(world, "meeting.agenda_set") == [{"count": 2}, {"count": 2}]


@pytest.mark.parametrize(
    ("titles", "code", "detail"),
    [
        (["A", "", "x" * 201], "meeting_item_too_long", "item:2"),
        ([f"Point {n}" for n in range(51)], "meeting_agenda_too_long", None),
    ],
)
def test_agenda_limits(
    world: World, owner: User, titles: list[str], code: str, detail: str | None
) -> None:
    public_id = world.assembly(owner)
    with pytest.raises(InvalidInputError) as raised, world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, titles)
    assert (raised.value.code, raised.value.detail) == (code, detail)
    set_agenda(world, owner, public_id, ["y" * 200] + [f"Point {n}" for n in range(49)])
    assert len(agenda_of(world, owner, public_id)[0]) == 50


def test_closed_session_refuses_agenda_changes(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    set_agenda(world, owner, public_id, ["Rapport moral", "Rapport financier"])
    set_current(world, owner, public_id, 2)
    with world.database.write() as db:
        assemblies.close_assembly(db, owner, public_id)
    calls = [
        lambda db: meeting.set_agenda(db, owner, public_id, ["Autre"]),
        lambda db: meeting.set_current_item(db, owner, public_id, 1),
        lambda db: meeting.move_current_item(db, owner, public_id, -1, seen=2),
    ]
    for call in calls:
        with pytest.raises(InvalidStateError) as raised, world.database.write() as db:
            call(db)
        assert raised.value.code == "assembly_closed"
    assert agenda_of(world, owner, public_id) == (["Rapport moral", "Rapport financier"], 2)


def test_agenda_editable_in_preparation(world: World, owner: User) -> None:
    public_id = world.assembly(owner, in_session=False)
    set_agenda(world, owner, public_id, ["Ouverture"])
    set_current(world, owner, public_id, 1)
    assert agenda_of(world, owner, public_id) == (["Ouverture"], 1)


def test_current_item_moves_and_bounds(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    set_agenda(world, owner, public_id, ["A", "B", "C"])
    move(world, owner, public_id, 1, seen=None)  # none → first
    assert agenda_of(world, owner, public_id)[1] == 1
    move(world, owner, public_id, 1, seen=1)
    move(world, owner, public_id, 1, seen=2)
    assert agenda_of(world, owner, public_id)[1] == 3
    for step, seen in ((1, 3), (-1, None)):
        if seen is None:
            set_current(world, owner, public_id, None)
        with pytest.raises(InvalidInputError) as raised, world.database.write() as db:
            meeting.move_current_item(db, owner, public_id, step, seen=seen)
        assert raised.value.code == "meeting_no_such_item"
    set_current(world, owner, public_id, 2)
    move(world, owner, public_id, -1, seen=2)
    assert agenda_of(world, owner, public_id)[1] == 1
    for position in (0, 4):
        with pytest.raises(InvalidInputError), world.database.write() as db:
            meeting.set_current_item(db, owner, public_id, position)
    assert agenda_of(world, owner, public_id)[1] == 1


def test_double_tap_never_skips_an_item(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    set_agenda(world, owner, public_id, ["A", "B", "C"])
    set_current(world, owner, public_id, 1)
    move(world, owner, public_id, 1, seen=1)
    move(world, owner, public_id, 1, seen=1)  # the second tap of a double tap: no change
    assert agenda_of(world, owner, public_id)[1] == 2
    set_current(world, owner, public_id, 2)  # the same item again: nothing logged
    assert entries(world, "meeting.agenda_item") == [{"position": 1}, {"position": 2}]
    set_current(world, owner, public_id, None)
    assert entries(world, "meeting.agenda_item")[-1] == {"position": None}


@pytest.mark.parametrize(
    ("old", "current", "new", "expected"),
    [
        (["A", "B", "C"], 2, ["A", "X", "B", "C"], 3),  # item inserted before: follows "B"
        (["A", "B", "C"], 2, ["B", "C"], 1),  # item removed before
        (["A", "B", "C"], 2, ["A", "Bis", "C"], 2),  # title corrected in place
        (["A", "B", "C"], 3, ["A", "B"], None),  # removed, position gone
        (["A", "B", "C"], 2, [], None),
        (["A", "B", "A", "C"], 3, ["A", "X", "B", "A", "C"], 4),  # nearest of two "A"
        (["A", "B"], None, ["A", "B", "C"], None),
    ],
)
def test_current_item_follows_agenda_edits(
    world: World,
    owner: User,
    old: list[str],
    current: int | None,
    new: list[str],
    expected: int | None,
) -> None:
    public_id = world.assembly(owner)
    set_agenda(world, owner, public_id, old)
    set_current(world, owner, public_id, current)
    set_agenda(world, owner, public_id, new)
    assert agenda_of(world, owner, public_id) == (new, expected)


def test_stored_position_out_of_range_is_dropped(world: World, owner: User) -> None:
    """A position that no longer matches the stored agenda never survives an edit."""
    public_id = world.assembly(owner)
    with world.database.write() as db:
        assembly = db.scalar(select(Assembly).where(Assembly.public_id == public_id))
        assert assembly is not None
        assembly.agenda, assembly.agenda_current = [{"title": "A"}], 7
    set_agenda(world, owner, public_id, ["A", "B"])
    assert agenda_of(world, owner, public_id) == (["A", "B"], None)
