import datetime as dt

import pytest
from sqlalchemy import func, select

from agaura.db import Database
from agaura.models import Poll, PollStatus, User, utcnow
from agaura.services import assemblies, polls
from agaura.services.errors import (
    InvalidInputError,
    InvalidStateError,
    NotFoundError,
    QuotaExceededError,
)
from agaura.services.polls import OptionInput, PollInput
from tests.conftest import RESOLUTION, TEST_AUDIT_KEY, World

CHOOSE = PollInput("Lieu", "single_choice", ["Salle", "Parc", "Ferme"])


def get(world: World, owner: User, poll_id: int) -> Poll:
    with world.database.read() as db:
        poll = polls.get_poll(db, owner, poll_id)
        _ = poll.options, poll.result
        return poll


class TestDrafts:
    def test_create(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False)
        data = PollInput(
            title=" Budget ",
            method="approval",
            options=[OptionInput(" Projet A ", "Détails"), "Projet B", "Projet C"],
            params={"seats": 2},
            description=" Contexte ",
        )
        poll = get(world, owner, world.poll(owner, public_id, data, open_it=False))
        assert (poll.title, poll.description, poll.status) == (
            "Budget",
            "Contexte",
            PollStatus.DRAFT,
        )
        assert [(o.position, o.label, o.description) for o in poll.options] == [
            (1, "Projet A", "Détails"),
            (2, "Projet B", None),
            (3, "Projet C", None),
        ]
        assert poll.blank_label is None  # the interface shows "Vote blanc"

    @pytest.mark.parametrize(
        ("data", "code"),
        [
            (PollInput("", "single_choice", ["A", "B"]), "required"),
            (PollInput("T", "single_choice", ["A", " "]), "required"),
            (PollInput("T", "single_choice", ["A", "a"]), "duplicate_option_label"),
            (PollInput("T", "single_choice", ["A"]), "option_count"),
            (PollInput("T", "borda", ["A", "B"]), "unknown_method"),
            (PollInput("T", "approval", ["A", "B"], {"seats": 3}), "invalid_param"),
            (PollInput("T", "single_choice", ["A", "B"], {"adopt_option": 3}), "invalid_param"),
            (PollInput("T", "single_choice", ["A", "B"], blank_label="x" * 101), "too_long"),
        ],
    )
    def test_invalid_definitions(
        self, world: World, owner: User, data: PollInput, code: str
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        with pytest.raises(InvalidInputError) as error:
            world.poll(owner, public_id, data, open_it=False)
        assert error.value.code == code

    @pytest.mark.parametrize("labels", [["Oui", "Non", "Peut-être"], ["Oui"] * 1 + ["Non"]])
    def test_update_changes_the_options(self, world: World, owner: User, labels: list[str]) -> None:
        public_id = world.assembly(owner, in_session=False)
        poll_id = world.poll(owner, public_id, CHOOSE, open_it=False)
        with world.database.write() as db:
            polls.update_poll(db, owner, poll_id, PollInput("Question", "single_choice", labels))
        poll = get(world, owner, poll_id)
        assert [o.label for o in poll.options] == labels
        assert [o.position for o in poll.options] == list(range(1, len(labels) + 1))

    def test_only_drafts_can_be_edited_or_deleted(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id)
        with world.database.write() as db:
            with pytest.raises(InvalidStateError, match="poll_open"):
                polls.update_poll(db, owner, poll_id, RESOLUTION)
            with pytest.raises(InvalidStateError, match="poll_open"):
                polls.delete_poll(db, owner, poll_id)
            draft = polls.create_poll(db, owner, public_id, CHOOSE)
            polls.delete_poll(db, owner, draft.id)
            assert db.get(Poll, draft.id) is None

    def test_no_new_poll_in_a_closed_session(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, public_id)
        with pytest.raises(InvalidStateError, match="assembly_closed"):
            world.poll(owner, public_id, open_it=False)

    def test_order(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        first, second, third = (world.poll(owner, public_id, open_it=False) for _ in range(3))
        with world.database.write() as db:
            polls.open_poll(db, owner, third)
            polls.reorder_drafts(db, owner, public_id, [second, first])
            listed = [poll.id for poll in polls.list_polls(db, owner, public_id)]
            for wrong in ([first], [first, second, third], [first, first]):
                with pytest.raises(InvalidInputError, match="invalid_order"):
                    polls.reorder_drafts(db, owner, public_id, wrong)
        assert listed == [third, second, first]  # pushed first, then drafts in their order

    def test_duplicate_and_copy(self, world: World, owner: User) -> None:
        past = world.assembly(owner)
        kept = world.poll(owner, past)
        cancelled = world.poll(owner, past, CHOOSE)
        with world.database.write() as db:
            polls.cancel_poll(db, owner, cancelled, "Erreur dans les options")
            copy = polls.duplicate_poll(db, owner, kept)
            assert copy.status is PollStatus.DRAFT and copy.id != kept
        new = world.assembly(owner, in_session=False)
        with world.database.write() as db:
            copies = polls.copy_polls(db, owner, past, new)
            assert [poll.title for poll in copies] == [RESOLUTION.title, RESOLUTION.title]
            assert all(poll.status is PollStatus.DRAFT for poll in copies)
            assert copies[0].params == {"adopt_option": 1}
            assert [o.label for o in copies[0].options] == ["Pour", "Contre"]

    def test_polls_quota(self, world: World) -> None:
        owner = world.user(polls_per_assembly=2)
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, open_it=False)
        world.poll(owner, public_id, open_it=False)
        with world.database.write() as db:
            for action in (
                lambda: polls.create_poll(db, owner, public_id, CHOOSE),
                lambda: polls.duplicate_poll(db, owner, poll_id),
            ):
                with pytest.raises(QuotaExceededError, match="polls_per_assembly"):
                    action()


class TestLifecycle:
    def test_push_close_publish_dismiss(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id)
        token = world.voters(owner, public_id, 1)[0]
        world.vote(public_id, token, poll_id, {"choice": 1})
        with world.database.write() as db:
            result = polls.close_poll(db, owner, poll_id)
            assert result.data["counts"] == {"cast": 1, "blank": 0, "valid": 1}
            assert result.data["outcome"]["adopted"] is True
            assert len(result.ballots_sha256) == 64
            poll = polls.publish_result(db, owner, poll_id, voters=True)
            assert (poll.status, poll.shown_to_voters, poll.shown_on_screen) == (
                PollStatus.PUBLISHED,
                True,
                False,  # the projection screen is a separate choice (FR-75)
            )
            assert poll.result is not None and poll.result.published_at is not None
            polls.show_on_screen(db, owner, poll_id, True)
            poll = polls.dismiss_result(db, owner, poll_id)
            assert (poll.shown_to_voters, poll.shown_on_screen) == (False, True)
            assert poll.status is PollStatus.PUBLISHED

    def test_push_needs_a_session_in_session(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False)
        poll_id = world.poll(owner, public_id, open_it=False)
        with (
            pytest.raises(InvalidStateError, match="assembly_preparation"),
            world.database.write() as db,
        ):
            polls.open_poll(db, owner, poll_id)

    @pytest.mark.parametrize("minutes", [0, 24 * 60 + 1])
    def test_timer_bounds(self, world: World, owner: User, minutes: int) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, open_it=False)
        with pytest.raises(InvalidInputError, match="invalid_timer"), world.database.write() as db:
            polls.open_poll(db, owner, poll_id, minutes=minutes)

    def test_forbidden_transitions(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        draft = world.poll(owner, public_id, open_it=False)
        opened = world.poll(owner, public_id)
        with world.database.write() as db:
            for action in (
                lambda: polls.open_poll(db, owner, opened),
                lambda: polls.close_poll(db, owner, draft),
                lambda: polls.cancel_poll(db, owner, draft, "raison"),
                lambda: polls.publish_result(db, owner, opened, voters=True),
                lambda: polls.dismiss_result(db, owner, opened),
            ):
                with pytest.raises(InvalidStateError):
                    action()
            polls.close_poll(db, owner, opened)
            with pytest.raises(InvalidStateError):
                polls.close_poll(db, owner, opened)
            with pytest.raises(InvalidInputError, match="no_target"):
                polls.publish_result(db, owner, opened, voters=False)

    def test_cancel(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        opened, published = world.poll(owner, public_id), world.poll(owner, public_id)
        with world.database.write() as db:
            with pytest.raises(InvalidInputError, match="required"):
                polls.cancel_poll(db, owner, opened, "  ")
            poll = polls.cancel_poll(db, owner, opened, " Mauvaise question ")
            assert (poll.status, poll.cancel_reason) == (PollStatus.CANCELLED, "Mauvaise question")
            assert poll.closed_at is not None and poll.result is None
            polls.close_poll(db, owner, published)
            polls.publish_result(db, owner, published, voters=True)
            polls.show_on_screen(db, owner, published, True)
            poll = polls.cancel_poll(db, owner, published, "Erreur de décompte")
            assert (poll.shown_to_voters, poll.shown_on_screen) == (False, False)
            assert poll.screen_shown_at is None
            with pytest.raises(InvalidStateError, match="poll_cancelled"):
                polls.cancel_poll(db, owner, published, "encore")


class TestScreen:
    """Results on the projection screen (FR-75): a choice of the tribune, separate from the
    voter page, for closed polls only, each display and removal logged."""

    def entries(self, world: World) -> list[tuple[str, dict[str, object]]]:
        from agaura.models import AuditEntry

        with world.database.read() as db:
            rows = db.scalars(
                select(AuditEntry)
                .where(AuditEntry.action.like("poll.screen_%"))
                .order_by(AuditEntry.id)
            ).all()
            return [(row.action, row.details) for row in rows]

    def test_show_then_take_off(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id)
        token = world.voters(owner, public_id, 1)[0]
        world.vote(public_id, token, poll_id, {"choice": 2})
        shown_at = dt.datetime(2026, 6, 20, 15, 0, tzinfo=dt.UTC)
        with world.database.write() as db:
            polls.close_poll(db, owner, poll_id)
            poll = polls.show_on_screen(db, owner, poll_id, True, now=shown_at)
            # Shown on the screen, not published: the voter page keeps its own choice.
            assert (poll.shown_on_screen, poll.screen_shown_at) == (True, shown_at)
            assert (poll.status, poll.shown_to_voters) == (PollStatus.CLOSED, False)
            assert poll.result is not None and poll.result.published_at is None
            polls.show_on_screen(db, owner, poll_id, True)  # already shown: nothing changes
            assert poll.screen_shown_at == shown_at
            poll = polls.show_on_screen(db, owner, poll_id, False)
            assert (poll.shown_on_screen, poll.screen_shown_at) == (False, None)
            polls.show_on_screen(db, owner, poll_id, False)
        # One entry per display and per removal, with the poll's id only (FR-80).
        assert self.entries(world) == [
            ("poll.screen_shown", {"poll_id": poll_id}),
            ("poll.screen_hidden", {"poll_id": poll_id}),
        ]

    def test_independent_of_the_voter_page(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id)
        with world.database.write() as db:
            polls.close_poll(db, owner, poll_id)
            polls.publish_result(db, owner, poll_id, voters=True)
            poll = polls.show_on_screen(db, owner, poll_id, True)
            assert (poll.shown_to_voters, poll.shown_on_screen) == (True, True)
            poll = polls.dismiss_result(db, owner, poll_id)
            assert (poll.shown_to_voters, poll.shown_on_screen) == (False, True)
            poll = polls.show_on_screen(db, owner, poll_id, False)
            assert (poll.status, poll.shown_to_voters) == (PollStatus.PUBLISHED, False)
            poll = polls.publish_result(db, owner, poll_id, voters=True)
            assert poll.shown_on_screen is False

    def test_never_an_open_draft_or_cancelled_poll(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        draft = world.poll(owner, public_id, open_it=False)
        opened = world.poll(owner, public_id)
        cancelled = world.poll(owner, public_id)
        with world.database.write() as db:
            polls.close_poll(db, owner, cancelled)
            polls.show_on_screen(db, owner, cancelled, True)
            poll = polls.cancel_poll(db, owner, cancelled, "Erreur de décompte")
            assert (poll.shown_on_screen, poll.screen_shown_at) == (False, None)
        for poll_id, code in (
            (draft, "poll_draft"),
            (opened, "poll_open"),
            (cancelled, "poll_cancelled"),
        ):
            for shown in (True, False):
                with pytest.raises(InvalidStateError, match=code), world.database.write() as db:
                    polls.show_on_screen(db, owner, poll_id, shown)
        assert [action for action, _ in self.entries(world)] == ["poll.screen_shown"]

    def test_account_isolation(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id)
        with world.database.write() as db:
            polls.close_poll(db, owner, poll_id)
        intruder = world.user("intruder@example.org")
        with pytest.raises(NotFoundError), world.database.write() as db:
            polls.show_on_screen(db, intruder, poll_id, True)
        assert get(world, owner, poll_id).shown_on_screen is False


class TestTimers:
    def test_expired_polls_close_automatically(self, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, open_it=False)
        untimed = world.poll(owner, public_id)
        start = utcnow()
        with world.database.write() as db:
            polls.open_poll(db, owner, poll_id, minutes=2, now=start)
            assert polls.close_expired_polls(db, now=start + dt.timedelta(minutes=1)) == []
        # A fresh Database object stands for a restart of the application.
        restarted = Database(str(world.database.url), audit_key=TEST_AUDIT_KEY)
        with restarted.write() as db:
            closed = polls.close_expired_polls(db, now=start + dt.timedelta(minutes=2))
            assert [poll.id for poll in closed] == [poll_id]
            assert closed[0].result is not None
        restarted.dispose()
        assert get(world, owner, untimed).status is PollStatus.OPEN
        with world.database.read() as db:
            from agaura.models import AuditEntry

            last = db.scalars(select(AuditEntry).order_by(AuditEntry.id.desc())).first()
            assert last is not None and (last.action, last.actor) == ("poll.closed", "system")


def test_other_accounts_cannot_reach_polls(world: World, owner: User) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id)
    intruder = world.user("intruder@example.org")
    other_session = world.assembly(intruder)
    with world.database.write() as db:
        for action in (
            lambda: polls.get_poll(db, intruder, poll_id),
            lambda: polls.close_poll(db, intruder, poll_id),
            lambda: polls.cancel_poll(db, intruder, poll_id, "raison"),
            lambda: polls.duplicate_poll(db, intruder, poll_id),
            lambda: polls.copy_polls(db, intruder, public_id, other_session),
        ):
            with pytest.raises(NotFoundError):
                action()
        assert db.scalar(select(func.count()).select_from(Poll)) == 1
