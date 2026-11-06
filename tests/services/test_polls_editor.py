"""Poll service functions behind the full editor: every problem at once, draft moves, copy from
another session of the account (FR-15, FR-30, FR-32)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from agaura.models import AuditEntry, Poll, PollStatus, User
from agaura.services import assemblies, polls
from agaura.services.errors import (
    InvalidInputError,
    InvalidStateError,
    NotFoundError,
    QuotaExceededError,
)
from agaura.services.polls import OptionInput, PollInput
from tests.conftest import RESOLUTION, World

CHOOSE = PollInput("Lieu", "single_choice", ["Salle", "Parc", "Ferme"])


def problems(data: PollInput) -> dict[str, str]:
    return {error.detail or "": error.code for error in polls.poll_problems(data)}


def draft_order(world: World, owner: User, public_id: str) -> list[int]:
    with world.database.read() as db:
        return [
            poll.id
            for poll in polls.list_polls(db, owner, public_id)
            if poll.status is PollStatus.DRAFT
        ]


def audit_count(world: World) -> int:
    with world.database.read() as db:
        return db.scalar(select(func.count()).select_from(AuditEntry)) or 0


class TestProblems:
    def test_a_valid_poll_has_none(self) -> None:
        assert polls.poll_problems(RESOLUTION) == []
        data = PollInput(
            "Budget",
            "approval",
            [OptionInput("A", "Détails"), "B", "C"],
            {"seats": 2, "max_choices": 2, "min_share": {"fraction": "1/2", "strict": False}},
        )
        assert polls.poll_problems(data) == []

    def test_every_problem_is_reported_with_its_field(self) -> None:
        data = PollInput(
            title=" ",
            method="single_choice",
            options=[OptionInput("Oui", "x" * 1001), OptionInput(" ", "Sans intitulé"), "oui"],
            params={"threshold": {"fraction": "3/2", "strict": False}},
            description="d" * 5001,
            blank_label="b" * 101,
        )
        assert problems(data) == {
            "title": "required",
            "description": "too_long",
            "option_description:1": "too_long",
            "option:2": "required",
            "options": "duplicate_option_label",
            "blank_label": "too_long",
            "threshold": "invalid_param",
        }

    @pytest.mark.parametrize(
        ("data", "field", "code"),
        [
            (PollInput("T", "single_choice", ["A"]), "options", "option_count"),
            (
                PollInput("T", "single_choice", [str(n) for n in range(31)]),
                "options",
                "option_count",
            ),
            (PollInput("T", "borda", ["A", "B"]), "method", "unknown_method"),
            (PollInput("T", "approval", ["A", "B"], {"seats": 3}), "seats", "invalid_param"),
            (
                PollInput("T", "approval", ["A", "B"], {"max_choices": 0}),
                "max_choices",
                "invalid_param",
            ),
            (
                PollInput("T", "single_choice", ["A", "B"], {"adopt_option": 3}),
                "adopt_option",
                "invalid_param",
            ),
            (
                PollInput("T", "single_choice", ["A", "B"], {"colour": "red"}),
                "params",
                "unknown_param",
            ),
            (
                PollInput(
                    "T", "approval", ["A", "B"], {"min_share": {"fraction": "1", "strict": True}}
                ),
                "min_share",
                "invalid_param",
            ),
        ],
    )
    def test_method_problems(self, data: PollInput, field: str, code: str) -> None:
        assert problems(data) == {field: code}

    def test_the_first_problem_is_raised_on_save(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False)
        with pytest.raises(InvalidInputError) as error:
            world.poll(owner, public_id, PollInput("", "single_choice", ["A", "A"]), open_it=False)
        assert (error.value.code, error.value.detail) == ("required", "title")

    def test_option_descriptions_and_labels_are_trimmed(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False)
        data = PollInput(
            " T ", "single_choice", [OptionInput(" A ", "  "), OptionInput("B", " d ")]
        )
        poll_id = world.poll(owner, public_id, data, open_it=False)
        with world.database.read() as db:
            poll = polls.get_poll(db, owner, poll_id)
            assert poll.title == "T"
            assert [(o.label, o.description) for o in poll.options] == [("A", None), ("B", "d")]


class TestEditing:
    def test_drafts_of_a_closed_session_cannot_be_edited(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, open_it=False)
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, public_id)
        with world.database.write() as db, pytest.raises(InvalidStateError, match="closed"):
            polls.update_poll(db, owner, poll_id, CHOOSE)


class TestMoves:
    def test_up_and_down(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False)
        first, second, third = (world.poll(owner, public_id, open_it=False) for _ in range(3))
        with world.database.write() as db:
            assert polls.move_draft(db, owner, third, "up")
        assert draft_order(world, owner, public_id) == [first, third, second]
        with world.database.write() as db:
            assert polls.move_draft(db, owner, first, "down")
        assert draft_order(world, owner, public_id) == [third, first, second]

    def test_bounds_change_nothing(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False)
        first, second = (world.poll(owner, public_id, open_it=False) for _ in range(2))
        before = audit_count(world)
        with world.database.write() as db:
            assert not polls.move_draft(db, owner, first, "up")
            assert not polls.move_draft(db, owner, second, "down")
        assert draft_order(world, owner, public_id) == [first, second]
        assert audit_count(world) == before

    def test_pushed_polls_are_not_counted(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        first, pushed, last = (world.poll(owner, public_id, open_it=False) for _ in range(3))
        with world.database.write() as db:
            polls.open_poll(db, owner, pushed)
            assert polls.move_draft(db, owner, last, "up")
            with pytest.raises(InvalidStateError, match="poll_open"):
                polls.move_draft(db, owner, pushed, "down")
        assert draft_order(world, owner, public_id) == [last, first]

    def test_invalid_moves(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, open_it=False)
        intruder = world.user("intruder@example.org")
        with world.database.write() as db:
            with pytest.raises(InvalidInputError, match="invalid_order"):
                polls.move_draft(db, owner, poll_id, "sideways")
            with pytest.raises(NotFoundError):
                polls.move_draft(db, intruder, poll_id, "up")
            assemblies.close_assembly(db, owner, public_id)
        with world.database.write() as db, pytest.raises(InvalidStateError, match="closed"):
            polls.move_draft(db, owner, poll_id, "up")


class TestCopy:
    def test_sources_are_the_other_sessions_with_polls(self, world: World, owner: User) -> None:
        older = world.assembly(owner, title="AG 2024")
        world.poll(owner, older)
        cancelled = world.poll(owner, older, CHOOSE)
        with world.database.write() as db:
            polls.cancel_poll(db, owner, cancelled, "Erreur")
        only_cancelled = world.assembly(owner, title="Annulée")
        gone = world.poll(owner, only_cancelled)
        with world.database.write() as db:
            polls.cancel_poll(db, owner, gone, "Erreur")
        world.assembly(owner, title="Vide")
        recent = world.assembly(owner, in_session=False, title="AG 2025")
        world.poll(owner, recent, open_it=False)
        world.poll(owner, recent, CHOOSE, open_it=False)
        intruder = world.user("intruder@example.org")
        world.poll(intruder, world.assembly(intruder, title="Autre compte"))
        target = world.assembly(owner, in_session=False, title="AG 2026")
        world.poll(owner, target, open_it=False)
        with world.database.read() as db:
            sources = polls.copy_sources(db, owner, target)
            listed = [(source.assembly.title, source.polls) for source in sources]
        assert listed == [("AG 2025", 2), ("AG 2024", 1)]

    def test_copies_come_after_the_drafts(self, world: World, owner: User) -> None:
        past = world.assembly(owner, in_session=False)
        a = world.poll(owner, past, CHOOSE, open_it=False)
        b = world.poll(owner, past, open_it=False)
        with world.database.write() as db:
            polls.move_draft(db, owner, b, "up")
        target = world.assembly(owner, in_session=False)
        existing = world.poll(owner, target, open_it=False)
        with world.database.write() as db:
            copies = [poll.id for poll in polls.copy_polls(db, owner, past, target.lower())]
        assert draft_order(world, owner, target) == [existing, *copies]
        with world.database.read() as db:
            titles = [db.get(Poll, poll_id).title for poll_id in copies]  # type: ignore[union-attr]
        assert titles == [RESOLUTION.title, CHOOSE.title]
        assert a not in copies

    def test_refusals(self, world: World, owner: User) -> None:
        target = world.assembly(owner, in_session=False)
        world.poll(owner, target, open_it=False)
        empty = world.assembly(owner, in_session=False)
        intruder = world.user("intruder@example.org")
        foreign = world.assembly(intruder)
        world.poll(intruder, foreign)
        before = audit_count(world)
        with world.database.write() as db:
            with pytest.raises(InvalidInputError, match="editor_copy_same_session"):
                polls.copy_polls(db, owner, target, target)
            with pytest.raises(InvalidInputError, match="editor_copy_empty"):
                polls.copy_polls(db, owner, empty, target)
            for unknown in (foreign, "ZZZZZ9", ""):
                with pytest.raises(NotFoundError):  # another account's session looks missing
                    polls.copy_polls(db, owner, unknown, target)
        assert audit_count(world) == before

    def test_quota(self, world: World) -> None:
        owner = world.user(polls_per_assembly=3)
        past = world.assembly(owner, in_session=False)
        for _ in range(2):
            world.poll(owner, past, open_it=False)
        target = world.assembly(owner, in_session=False)
        world.poll(owner, target, open_it=False)
        world.poll(owner, target, open_it=False)
        with world.database.write() as db, pytest.raises(QuotaExceededError):
            polls.copy_polls(db, owner, past, target)
        assert len(draft_order(world, owner, target)) == 2
