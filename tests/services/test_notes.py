"""Session notes: limits, internal flag, agenda item, edits after closing, owner isolation."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from agaura.models import AuditEntry, Note, User
from agaura.services import assemblies, audit, meeting, notes
from agaura.services.errors import InvalidInputError, NotFoundError
from tests.conftest import World

T0 = dt.datetime(2026, 6, 20, 12, 0, tzinfo=dt.UTC)


def add(world: World, owner: User, public_id: str, text: str, **options: object) -> int:
    with world.database.write() as db:
        return notes.add_note(db, owner, public_id, text, **options).id  # type: ignore[arg-type]


def listed(world: World, owner: User, public_id: str) -> list[Note]:
    with world.database.read() as db:
        return notes.list_notes(db, owner, public_id)


def note_actions(world: World) -> list[tuple[str, dict]]:
    with world.database.read() as db:
        entries = db.scalars(select(AuditEntry).where(AuditEntry.action.like("note.%")))
        return [(entry.action, entry.details) for entry in entries]


def test_add_keeps_line_breaks_and_trims(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    add(world, owner, public_id, "  Première ligne\r\nDeuxième ligne\r\n\r\n  ", now=T0)
    (note,) = listed(world, owner, public_id)
    assert note.text == "Première ligne\nDeuxième ligne"
    assert (note.created_at, note.updated_at, note.internal) == (T0, None, False)
    assert (note.item_position, note.item_title) == (None, None)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "notes_text_required"),
        (" \n\t ", "notes_text_required"),
        ("x" * 5001, "notes_text_too_long"),
    ],
)
def test_text_limits(world: World, owner: User, text: str, code: str) -> None:
    public_id = world.assembly(owner)
    with pytest.raises(InvalidInputError) as raised, world.database.write() as db:
        notes.add_note(db, owner, public_id, text)
    assert raised.value.code == code
    add(world, owner, public_id, "y" * 5000)  # the bound itself is accepted
    add(world, owner, public_id, "a\r\n" * 2500)  # CRLF counted as one character


def test_at_most_500_notes(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    with world.database.write() as db:
        assembly = assemblies.get_assembly(db, owner, public_id)
        db.add_all(Note(assembly_id=assembly.id, text=f"n{n}") for n in range(500))
    with pytest.raises(InvalidInputError) as raised, world.database.write() as db:
        notes.add_note(db, owner, public_id, "Une de trop")
    assert raised.value.code == "notes_too_many"


def test_attached_to_the_item_under_discussion(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, ["Ouverture", "Rapport moral"])
        meeting.set_current_item(db, owner, public_id, 2)
    first = add(world, owner, public_id, "Le rapport est présenté.", now=T0)
    with world.database.write() as db:
        meeting.set_current_item(db, owner, public_id, None)
    add(world, owner, public_id, "Pause.", internal=True, now=T0 + dt.timedelta(minutes=1))
    found = listed(world, owner, public_id)
    assert [(note.id, note.item_position, note.item_title) for note in found] == [
        (first, 2, "Rapport moral"),
        (found[1].id, None, None),
    ]
    assert found[1].internal
    # The agenda edited later: the note follows its item by title.
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, ["Ouverture", "Accueil", "Rapport moral"])
    assert notes.note_item(found[0], ["Ouverture", "Accueil", "Rapport moral"]) == 3
    assert notes.note_item(found[0], ["Ouverture", "Rapport d'activité"]) == 2  # renamed in place
    assert notes.note_item(found[0], ["Ouverture"]) is None  # gone


def test_public_notes_leave_out_internal_ones(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    add(world, owner, public_id, "Publique", now=T0)
    add(world, owner, public_id, "Interne", internal=True, now=T0)
    with world.database.read() as db:
        assembly = assemblies.get_assembly(db, owner, public_id)
        assert [note.text for note in notes.session_notes(db, assembly, internal=False)] == [
            "Publique"
        ]
        assert len(notes.session_notes(db, assembly)) == 2


def test_update_text_internal_and_item(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, ["Ouverture", "Rapport moral"])
    note_id = add(world, owner, public_id, "Brouillon", now=T0)
    later = T0 + dt.timedelta(hours=1)
    with world.database.write() as db:
        notes.update_note(
            db, owner, public_id, note_id, " Corrigé\r\n", internal=True, item=2, now=later
        )
    (note,) = listed(world, owner, public_id)
    assert (note.text, note.internal, note.item_position, note.item_title) == (
        "Corrigé",
        True,
        2,
        "Rapport moral",
    )
    assert (note.created_at, note.updated_at) == (T0, later)
    with world.database.write() as db:
        notes.update_note(db, owner, public_id, note_id, "Corrigé", internal=False, item=None)
    assert listed(world, owner, public_id)[0].item_title is None
    for item, text in ((3, "Corrigé"), (1, "")):
        with pytest.raises(InvalidInputError), world.database.write() as db:
            notes.update_note(db, owner, public_id, note_id, text, internal=False, item=item)


def test_changes_are_logged_only_after_closing_and_never_the_text(
    world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    during = add(world, owner, public_id, "Pendant la séance")
    with world.database.write() as db:
        notes.update_note(db, owner, public_id, during, "Corrigée", internal=False, item=None)
        assemblies.close_assembly(db, owner, public_id)
    assert note_actions(world) == []
    after = add(world, owner, public_id, "Ajout au procès-verbal")
    with world.database.write() as db:
        notes.update_note(db, owner, public_id, during, "Corrigée encore", internal=True, item=None)
        notes.delete_note(db, owner, public_id, after)
    assert note_actions(world) == [
        ("note.added", {"note_id": after}),
        ("note.edited", {"note_id": during}),
        ("note.deleted", {"note_id": after}),
    ]
    assert [note.text for note in listed(world, owner, public_id)] == ["Corrigée encore"]


def test_another_account_or_session_gets_not_found(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    other_session = world.assembly(owner)
    note_id = add(world, owner, public_id, "Secret de la tribune")
    intruder = world.user("intruder@example.org")
    calls = [
        lambda db: notes.add_note(db, intruder, public_id, "x"),
        lambda db: notes.list_notes(db, intruder, public_id),
        lambda db: notes.get_note(db, intruder, public_id, note_id),
        lambda db: notes.update_note(
            db, intruder, public_id, note_id, "x", internal=False, item=None
        ),
        lambda db: notes.delete_note(db, intruder, public_id, note_id),
        # The owner, through another of their sessions.
        lambda db: notes.update_note(
            db, owner, other_session, note_id, "x", internal=False, item=None
        ),
        lambda db: notes.delete_note(db, owner, other_session, note_id),
        lambda db: notes.get_note(db, owner, public_id, 999_999),
    ]
    for call in calls:
        with pytest.raises(NotFoundError), world.database.write() as db:
            call(db)
    assert [note.text for note in listed(world, owner, public_id)] == ["Secret de la tribune"]


def test_deleting_the_session_deletes_its_notes(world: World, owner: User) -> None:
    public_id = world.assembly(owner, in_session=False)
    add(world, owner, public_id, "Préparation")
    with world.database.write() as db:
        assemblies.delete_assembly(db, owner, public_id)
    with world.database.read() as db:
        assert db.scalars(select(Note)).all() == []


def test_poll_records_the_item_under_discussion(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    with world.database.write() as db:
        meeting.set_agenda(db, owner, public_id, ["Ouverture", "Budget"])
        meeting.set_current_item(db, owner, public_id, 2)
    with_item = world.poll(owner, public_id)
    with world.database.write() as db:
        meeting.set_current_item(db, owner, public_id, None)
    without = world.poll(owner, public_id)
    with world.database.write() as db:  # an entry without a poll id is ignored
        assembly = assemblies.get_assembly(db, owner, public_id)
        audit.record(db, "poll.opened", assembly_id=assembly.id, details={"poll_id": "x"})
    with world.database.read() as db:
        assembly = assemblies.get_assembly(db, owner, public_id)
        assert meeting.poll_items(db, assembly) == {with_item: (2, "Budget"), without: (None, None)}
