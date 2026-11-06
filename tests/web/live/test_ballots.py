"""Ballots of the four methods: summary, confirmation, blank votes, errors (SPEC §6.5,
FR-43 to FR-45). Without JavaScript (plain forms) and with it (section fragments)."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agaura.models import User
from agaura.services import polls
from agaura.services.polls import PollInput
from agaura.web.security import CSRF_COOKIE
from tests.conftest import RESOLUTION, World
from tests.web.conftest import post
from tests.web.live.conftest import (
    APPROVAL,
    CONDORCET,
    MAJORITY_JUDGMENT,
    ballots_of,
    join,
    poll_action,
)

FETCH = {"X-Requested-With": "fetch"}
BLANK_WITH_CHOICE_FR = "Un vote blanc ne se combine avec aucun autre choix."


@pytest.fixture
def voter(client: TestClient, world: World, owner: User, session_id: str) -> TestClient:
    join(client, world, owner, session_id)
    return client


def review(client: TestClient, public_id: str, poll_id: int, data: dict[str, Any], **kw: Any):
    return post(client, f"/{public_id}/polls/{poll_id}/review", data, **kw)


def cast(client: TestClient, public_id: str, poll_id: int, data: dict[str, Any], **kw: Any):
    return post(client, f"/{public_id}/polls/{poll_id}", data, **kw)


def hidden_fields(html: str) -> list[tuple[str, str]]:
    return [
        (name, value)
        for name, value in re.findall(r'type="hidden" name="([^"]+)" value="([^"]*)"', html)
        if name != "csrf_token"
    ]


def vote_through_summary(
    client: TestClient, public_id: str, poll_id: int, data: dict[str, Any]
) -> str:
    """Ballot form → summary → confirmation, as a browser without JavaScript does."""
    summary = review(client, public_id, poll_id, data)
    assert summary.status_code == 200, summary.text
    assert "Vérifiez votre vote" in summary.text
    fields: dict[str, Any] = {}
    for name, value in hidden_fields(summary.text):
        fields.setdefault(name, []).append(value)
    confirmed = cast(client, public_id, poll_id, fields)
    assert confirmed.status_code == 303 and confirmed.headers["location"] == f"/{public_id}"
    return summary.text


class TestSingleChoice:
    def test_vote(self, voter: TestClient, world: World, owner: User, session_id: str) -> None:
        poll_id = world.poll(owner, session_id)
        summary = vote_through_summary(voter, session_id, poll_id, {"choice": "1"})
        assert "Pour" in summary and "Confirmer mon vote" in summary
        assert ballots_of(world, poll_id) == [(False, {"choice": 1})]
        page = voter.get(f"/{session_id}").text
        assert "Votre vote a été enregistré." in page  # the notice of the redirect
        assert f'data-key="poll-{poll_id}-recorded"' in page and "· vote enregistré" in page

    def test_blank(self, voter: TestClient, world: World, owner: User, session_id: str) -> None:
        poll_id = world.poll(owner, session_id)
        summary = vote_through_summary(voter, session_id, poll_id, {"choice": "00"})
        # The words of the ballot's blank choice, unchanged (FR-43).
        assert '<p class="summary-main">Abstention</p>' in summary
        assert "blanc" not in summary
        assert ballots_of(world, poll_id) == [(True, None)]

    def test_blank_is_one_more_radio_after_the_options(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        """SPEC §6.6: a row of the options' group, drawn like them, the poll's blank label."""
        world.poll(owner, session_id)
        page = voter.get(f"/{session_id}").text
        rows = re.findall(r'<label class="choice"><input type="radio" name="choice" [^>]*>', page)
        assert [re.search(r'value="(\w+)"', row)[1] for row in rows] == ["01", "02", "00"]  # type: ignore[index]
        blank = '<input type="radio" name="choice" value="00" data-blank><span class="choice-text">'
        assert f"{blank}Abstention</span></label>\n</fieldset>" in page

    def test_blank_with_a_choice_is_refused(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        """Only a hand-made request can send both: the blank is a radio button of the group."""
        poll_id = world.poll(owner, session_id)
        for send in (review, cast):
            response = send(voter, session_id, poll_id, {"choice": "1", "blank": "1"})
            assert response.status_code == 422 and BLANK_WITH_CHOICE_FR in response.text
            assert 'name="choice" value="01" checked' in response.text
        assert ballots_of(world, poll_id) == []

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            ({}, "Choisissez au moins une option, ou votez blanc."),
            ({"choice": "abc"}, "Ce bulletin n&#39;a pas pu être lu."),
            ({"choice": "7"}, "Ce bulletin contient une option inconnue."),
        ],
    )
    def test_invalid(
        self,
        voter: TestClient,
        world: World,
        owner: User,
        session_id: str,
        data: dict[str, Any],
        message: str,
    ) -> None:
        poll_id = world.poll(owner, session_id)
        response = review(voter, session_id, poll_id, data)
        assert response.status_code == 422 and message in response.text
        assert cast(voter, session_id, poll_id, data).status_code == 422
        assert ballots_of(world, poll_id) == []

    def test_change_from_the_summary(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        poll_id = world.poll(owner, session_id)
        response = review(voter, session_id, poll_id, {"choice": "2", "action": "edit"})
        assert response.status_code == 200
        assert 'name="choice" value="02" checked' in response.text
        assert 'name="choice" value="01" checked' not in response.text


class TestApproval:
    def test_vote(self, voter: TestClient, world: World, owner: User, session_id: str) -> None:
        poll_id = world.poll(owner, session_id, APPROVAL)
        summary = vote_through_summary(voter, session_id, poll_id, {"approved": ["3", "1"]})
        assert "Alice" in summary and "Chloé" in summary and "Bruno" not in summary
        assert ballots_of(world, poll_id) == [(False, {"approved": [1, 3]})]

    def test_nothing_checked_is_a_blank_vote(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        poll_id = world.poll(owner, session_id, APPROVAL)
        summary = vote_through_summary(voter, session_id, poll_id, {})
        assert "Aucune option cochée" in summary
        assert '<p class="summary-main">Vote blanc</p>' in summary
        assert ballots_of(world, poll_id) == [(True, None)]

    def test_blank_box(self, voter: TestClient, world: World, owner: User, session_id: str) -> None:
        poll_id = world.poll(owner, session_id, APPROVAL)
        page = voter.get(f"/{session_id}").text
        box = page.index('type="checkbox" name="blank" value="1" data-blank')
        assert page.index('name="approved" value="03"') < box < page.index("Continuer", box)
        summary = vote_through_summary(voter, session_id, poll_id, {"blank": "1"})
        assert "Aucune option cochée" not in summary
        assert '<p class="summary-main">Vote blanc</p>' in summary
        assert hidden_fields(summary) == [("blank", "1")]
        assert ballots_of(world, poll_id) == [(True, None)]

    def test_blank_with_options_is_refused_and_the_options_kept(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        """Without JavaScript nothing unchecks the other boxes: the server refuses (FR-44)."""
        poll_id = world.poll(owner, session_id, APPROVAL)
        data = {"approved": ["1", "3"], "blank": "1"}
        for send in (review, cast):
            for headers in ({}, FETCH):
                response = send(voter, session_id, poll_id, data, headers=headers)
                assert response.status_code == 422 and BLANK_WITH_CHOICE_FR in response.text
                assert 'name="approved" value="01" checked' in response.text
                assert 'name="approved" value="03" checked' in response.text
                assert 'name="blank" value="1" data-blank checked' not in response.text
        edit = review(voter, session_id, poll_id, {**data, "action": "edit"})
        assert edit.status_code == 200 and BLANK_WITH_CHOICE_FR not in edit.text
        assert ballots_of(world, poll_id) == []

    def test_a_blank_summary_sent_back_keeps_the_box_checked(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        poll_id = world.poll(owner, session_id, APPROVAL)
        response = review(voter, session_id, poll_id, {"blank": "1", "action": "edit"})
        assert 'name="blank" value="1" data-blank checked' in response.text
        nothing = review(voter, session_id, poll_id, {"action": "edit"})  # implicit blank
        assert "data-blank checked" not in nothing.text

    def test_too_many_choices_keeps_the_ballot(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        poll_id = world.poll(owner, session_id, APPROVAL)
        response = review(voter, session_id, poll_id, {"approved": ["1", "2", "3"]})
        assert response.status_code == 422 and "Trop d&#39;options cochées." in response.text
        for position in (1, 2, 3):
            assert f'name="approved" value="{position:02d}" checked' in response.text
        assert cast(voter, session_id, poll_id, {"approved": ["1", "2", "3"]}).status_code == 422
        assert ballots_of(world, poll_id) == []


class TestMajorityJudgment:
    def test_vote_with_an_ungraded_option(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        poll_id = world.poll(owner, session_id, MAJORITY_JUDGMENT)
        summary = vote_through_summary(
            voter, session_id, poll_id, {"grade-01": "0", "grade-02": "3", "grade-03": ""}
        )
        assert "Très bien" in summary and "Passable" in summary
        assert "À rejeter" in summary and "(sans mention)" in summary
        assert (
            "Banc n'a pas de mention\u00a0: cette option comptera «\u00a0À rejeter\u00a0»."
            in summary
        )
        assert ballots_of(world, poll_id) == [(False, {"grades": {"1": 0, "2": 3, "3": 5}})]

    def test_the_summary_names_every_ungraded_option(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        """SPEC §6.4: the rule stays, the summary warns before the ballot is final."""
        poll_id = world.poll(owner, session_id, MAJORITY_JUDGMENT)
        url = f"/{session_id}/polls/{poll_id}/review"
        warning = re.compile(r'<p class="alert alert-warning ungraded" id="([^"]+)">([^<]*)</p>')
        for headers in ({}, FETCH):
            summary = post(voter, url, {"grade-01": "2"}, headers=headers).text
            found = warning.search(summary)
            assert found is not None
            assert found[2] == (
                "Fresque et Banc n'ont pas de mention\u00a0: "
                "ces options compteront «\u00a0À rejeter\u00a0»."
            )
            assert f'aria-describedby="{found[1]}">Confirmer mon vote' in summary
            assert summary.index(found[0]) < summary.index("Modifier mon vote")
        three = PollInput(
            "Trois", "majority_judgment", ["Jardin", "Fresque murale", "Atelier vélo"]
        )
        other = world.poll(owner, session_id, three)
        summary = review(voter, session_id, other, {"grade-03": "0"}).text
        assert "Jardin et Fresque murale n'ont pas de mention" in summary
        everything = {"grade-01": "0", "grade-02": "1", "grade-03": "4"}
        summary = review(voter, session_id, other, everything).text
        assert "ungraded" not in summary and "pas de mention" not in summary
        four = PollInput("Quatre", "majority_judgment", ["A", "B", "C", "D"])
        summary = review(voter, session_id, world.poll(owner, session_id, four), {"grade-04": "1"})
        assert "A, B et C n'ont pas de mention" in summary.text

    def test_blank(self, voter: TestClient, world: World, owner: User, session_id: str) -> None:
        poll_id = world.poll(owner, session_id, MAJORITY_JUDGMENT)
        page = voter.get(f"/{session_id}").text
        box = page.index('type="checkbox" name="blank" value="1" data-blank')
        assert page.rindex('name="grade-03"', 0, box) < box < page.index("Continuer", box)
        summary = vote_through_summary(voter, session_id, poll_id, {"blank": "1"})
        assert '<p class="summary-main">Vote blanc</p>' in summary
        assert "pas de mention" not in summary
        assert ballots_of(world, poll_id) == [(True, None)]

    def test_blank_with_grades_is_refused_and_the_grades_kept(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        poll_id = world.poll(owner, session_id, MAJORITY_JUDGMENT)
        for send in (review, cast):
            response = send(voter, session_id, poll_id, {"grade-02": "1", "blank": "1"})
            assert response.status_code == 422 and BLANK_WITH_CHOICE_FR in response.text
            assert 'name="grade-02" value="1" checked' in response.text
        assert ballots_of(world, poll_id) == []

    def test_counter_of_graded_options(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        poll_id = world.poll(owner, session_id, MAJORITY_JUDGMENT)
        page = voter.get(f"/{session_id}").text
        assert 'role="status" data-graded' in page
        assert 'data-one="{n} option sur 3 évaluée"' in page
        assert 'data-other="{n} options sur 3 évaluées" hidden>0 option sur 3 évaluée</p>' in page
        edit = {"grade-01": "1", "grade-03": "2", "action": "edit"}
        sent_back = review(voter, session_id, poll_id, edit).text
        assert "hidden>2 options sur 3 évaluées</p>" in sent_back

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            ({}, "Attribuez une mention à au moins une option, ou votez blanc."),
            ({"grade-01": "9"}, "Une mention est invalide."),
        ],
    )
    def test_invalid(
        self,
        voter: TestClient,
        world: World,
        owner: User,
        session_id: str,
        data: dict[str, Any],
        message: str,
    ) -> None:
        poll_id = world.poll(owner, session_id, MAJORITY_JUDGMENT)
        response = review(voter, session_id, poll_id, data)
        assert response.status_code == 422 and message in response.text


def summary_ranks(html: str) -> list[tuple[str, str]]:
    """(rank, option) of a Condorcet summary, in order."""
    found = re.search(r'<ol class="summary-ranks">(.*?)</ol>', html, re.S)
    assert found is not None, html
    return re.findall(
        r'<span class="rank[^"]*">([^<]*)</span> <span class="rank-label">([^<]*)</span>', found[1]
    )


class TestCondorcet:
    def test_vote_with_shared_ranks_and_an_unranked_option(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        poll_id = world.poll(owner, session_id, CONDORCET)
        page = voter.get(f"/{session_id}").text
        assert '<select id="poll-' in page and 'name="rank-04"' in page
        assert '<option value="00">Non classée</option>' in page
        assert '<option value="01">1er choix</option>' in page
        assert '<option value="04">4e choix</option>' in page
        summary = vote_through_summary(
            voter,
            session_id,
            poll_id,
            {"rank-01": "3", "rank-02": "1", "rank-03": "3", "rank-04": ""},
        )
        assert summary_ranks(summary) == [
            ("1er choix", "Écho"),
            ("3e choix", "Gazette"),
            ("3e choix", "Lien"),
            ("Non classée", "Fil"),
        ]
        assert '<span class="rank is-default">Non classée</span>' in summary
        assert "Les options non classées passent après les autres" in summary
        # Normalized: dense ranks, the unranked option at the last one.
        assert ballots_of(world, poll_id) == [(False, {"ranks": {"1": 2, "2": 1, "3": 2, "4": 3}})]

    def test_every_option_ranked_needs_no_hint(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        poll_id = world.poll(owner, session_id, CONDORCET)
        ranks = {"rank-01": "04", "rank-02": "03", "rank-03": "02", "rank-04": "01"}
        summary = review(voter, session_id, poll_id, ranks).text
        assert [option for _, option in summary_ranks(summary)] == [
            "Fil",
            "Lien",
            "Écho",
            "Gazette",
        ]
        assert "non classées" not in summary and "is-default" not in summary
        assert sorted(hidden_fields(summary)) == sorted(ranks.items())

    def test_change_from_the_summary_keeps_the_ranks(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        poll_id = world.poll(owner, session_id, CONDORCET)
        sent_back = review(voter, session_id, poll_id, {"rank-02": "1", "action": "edit"}).text
        assert re.search(
            r'name="rank-02">.*?<option value="01" selected>1er choix</option>', sent_back, re.S
        )
        assert sent_back.count(" selected>") == 1
        assert ballots_of(world, poll_id) == []

    def test_blank(self, voter: TestClient, world: World, owner: User, session_id: str) -> None:
        poll_id = world.poll(owner, session_id, CONDORCET)
        page = voter.get(f"/{session_id}").text
        box = page.index('type="checkbox" name="blank" value="1" data-blank')
        assert page.rindex('name="rank-04"', 0, box) < box < page.index("Continuer", box)
        summary = vote_through_summary(voter, session_id, poll_id, {"blank": "1", "rank-01": ""})
        assert '<p class="summary-main">Vote blanc</p>' in summary
        assert ballots_of(world, poll_id) == [(True, None)]

    def test_blank_with_ranks_is_refused_and_the_ranks_kept(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        poll_id = world.poll(owner, session_id, CONDORCET)
        for send in (review, cast):
            response = send(voter, session_id, poll_id, {"rank-03": "2", "blank": "1"})
            assert response.status_code == 422 and BLANK_WITH_CHOICE_FR in response.text
            assert '<option value="02" selected>2e choix</option>' in response.text
        assert ballots_of(world, poll_id) == []

    def test_counter_of_ranked_options(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        world.poll(owner, session_id, CONDORCET)
        page = voter.get(f"/{session_id}").text
        assert 'role="status" data-ranked data-total="4"' in page
        assert 'data-one="{n} option sur 4 classée"' in page
        assert 'data-other="{n} options sur 4 classées" hidden></p>' in page

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            ({}, "Classez au moins une option, ou votez blanc."),
            ({"rank-01": "", "rank-02": ""}, "Classez au moins une option, ou votez blanc."),
            ({"rank-01": "5"}, "Un rang est invalide."),
            ({"rank-01": "0"}, "Un rang est invalide."),
        ],
    )
    def test_invalid(
        self,
        voter: TestClient,
        world: World,
        owner: User,
        session_id: str,
        data: dict[str, Any],
        message: str,
    ) -> None:
        poll_id = world.poll(owner, session_id, CONDORCET)
        response = review(voter, session_id, poll_id, data)
        assert response.status_code == 422 and message in response.text
        assert ballots_of(world, poll_id) == []


class TestRefusals:
    def test_second_vote(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        poll_id = world.poll(owner, session_id)
        vote_through_summary(voter, session_id, poll_id, {"choice": "1"})
        again = cast(voter, session_id, poll_id, {"choice": "2"})
        assert again.status_code == 303
        assert "Vote déjà enregistré." in voter.get(f"/{session_id}").text
        inline = cast(voter, session_id, poll_id, {"choice": "2"}, headers=FETCH)
        assert inline.status_code == 409 and "Vote déjà enregistré." in inline.text
        assert f'data-key="poll-{poll_id}-voted"' in inline.text
        assert review(voter, session_id, poll_id, {"choice": "2"}).status_code == 303
        assert ballots_of(world, poll_id) == [(False, {"choice": 1})]

    def test_closed_poll(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        poll_id = world.poll(owner, session_id)
        assert review(voter, session_id, poll_id, {"choice": "1"}).status_code == 200
        poll_action(world, owner, "close_poll", poll_id)
        response = cast(voter, session_id, poll_id, {"choice": "1"})
        assert response.status_code == 409 and "Le vote est clos." in response.text
        assert review(voter, session_id, poll_id, {"choice": "1"}).status_code == 409
        assert ballots_of(world, poll_id) == []

    def test_draft_and_foreign_polls_are_not_found(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        draft = world.poll(owner, session_id, open_it=False)
        elsewhere = world.assembly(owner, title="Autre session")
        foreign = world.poll(owner, elsewhere)
        for poll_id in (draft, foreign, 999_999):
            assert cast(voter, session_id, poll_id, {"choice": "1"}).status_code == 404
            assert review(voter, session_id, poll_id, {"choice": "1"}).status_code == 404

    def test_without_credential(
        self, client: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        poll_id = world.poll(owner, session_id)
        response = cast(client, session_id, poll_id, {"choice": "1"})
        assert response.status_code == 303 and response.headers["location"] == f"/{session_id}"
        assert cast(client, session_id, poll_id, {"choice": "1"}, headers=FETCH).status_code == 401
        assert review(client, session_id, poll_id, {}, headers=FETCH).status_code == 401
        assert ballots_of(world, poll_id) == []

    def test_csrf_is_required(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        poll_id = world.poll(owner, session_id)
        response = voter.post(f"/{session_id}/polls/{poll_id}", data={"choice": "1"})
        assert response.status_code == 403
        assert ballots_of(world, poll_id) == []


class TestWithJavaScript:
    def test_review_and_confirm_inline(
        self,
        voter: TestClient,
        app: FastAPI,
        world: World,
        owner: User,
        session_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        published: list[tuple[str, ...]] = []
        monkeypatch.setattr(app.state.hub, "publish", lambda *args: published.append(args))
        poll_id = world.poll(owner, session_id, APPROVAL)
        headers = {**FETCH, "X-CSRF-Token": voter.cookies[CSRF_COOKIE]}
        url = f"/{session_id}/polls/{poll_id}"

        summary = voter.post(f"{url}/review", data={"approved": ["2"]}, headers=headers)
        assert summary.status_code == 200 and "<html" not in summary.text
        assert summary.text.lstrip().startswith("<section")
        assert f'data-key="poll-{poll_id}-open"' in summary.text and "data-focus" in summary.text
        assert hidden_fields(summary.text) == [("approved", "02")]

        done = voter.post(url, data={"approved": ["2"]}, headers=headers)
        assert done.status_code == 200 and "<html" not in done.text
        assert f'data-key="poll-{poll_id}-voted"' in done.text
        assert "Vote enregistré" in done.text and "Bruno" not in done.text
        assert "Votre vote a été enregistré." not in done.text  # the heading says it once
        assert ballots_of(world, poll_id) == [(False, {"approved": [2]})]
        assert published == [(f"manage:{session_id}", "turnout")]

    def test_errors_come_back_as_sections(
        self, voter: TestClient, world: World, owner: User, session_id: str
    ) -> None:
        poll_id = world.poll(owner, session_id, APPROVAL)
        response = review(voter, session_id, poll_id, {"approved": ["1", "2", "3"]}, headers=FETCH)
        assert response.status_code == 422 and "<html" not in response.text
        assert "Trop d&#39;options cochées." in response.text
        poll_action(world, owner, "close_poll", poll_id)
        closed = cast(voter, session_id, poll_id, {"approved": ["1"]}, headers=FETCH)
        assert closed.status_code == 409 and f'data-key="poll-{poll_id}-closed"' in closed.text


# --- Around the choice: the blank, counters, steps, flag, default grade, confirmation -------


def steps_of(html: str) -> list[tuple[str, str]]:
    """Attributes and text of each step of the ballot box."""
    found = re.search(r'<ol class="stepper" aria-label="Étapes du vote">(.*?)</ol>', html, re.S)
    assert found is not None, html
    return [
        (attrs.strip(), text) for attrs, text in re.findall(r"<li([^>]*)>([^<]*)</li>", found[1])
    ]


# How each method draws an option, and its blank vote the same way (SPEC §6.6).
BLANK_ROWS = {
    "single_choice": (
        '<label class="choice"><input type="radio" name="choice" value="02"',
        '<label class="choice"><input type="radio" name="choice" value="00" data-blank>'
        '<span class="choice-text">Abstention</span></label>',
    ),
    "approval": (
        '<label class="choice"><input type="checkbox" aria-describedby',
        '<label class="choice"><input type="checkbox" name="blank" value="1" data-blank>'
        '<span class="choice-text">Vote blanc</span></label>',
    ),
    "majority_judgment": (
        '<label class="grade-choice"><input type="radio" name="grade-03"',
        '<label class="grade-choice"><input type="checkbox" name="blank" value="1" data-blank>'
        '<span class="grade-pill">Vote blanc</span></label>',
    ),
    "condorcet": (
        '<div class="rank-option">',
        '<label class="rank-option rank-blank"><span class="rank-name">Vote blanc</span>'
        '<input type="checkbox" name="blank" value="1" data-blank></label>',
    ),
}


@pytest.mark.parametrize("data", [RESOLUTION, APPROVAL, MAJORITY_JUDGMENT, CONDORCET], ids=str)
def test_the_blank_is_the_last_choice_drawn_like_the_options(
    voter: TestClient, world: World, owner: User, session_id: str, data: PollInput
) -> None:
    """SPEC §6.1, §6.6: the blank vote right after the last option, at its level and drawn like
    it, with no rule or word that sets it apart; the counters (a status) right after it."""
    poll_id = world.poll(owner, session_id, data)
    page = voter.get(f"/{session_id}").text
    section = page[
        page.index(f'id="poll-{poll_id}"') : page.index(
            "</form>", page.index(f'id="poll-{poll_id}"')
        )
    ]
    option, row = BLANK_ROWS[data.method]
    blank = section.index(row)
    assert section.count("data-blank") == 1 and section.rindex(option) < blank
    # The last value before the blank: a position or a rank on two digits, a grade on one.
    count = len(data.options)
    last = f'value="{count}"' if data.method == "majority_judgment" else f'value="{count:02d}"'
    assert section.rindex(last, 0, blank) < blank
    between = section[section.index(">", section.rindex(last)) : blank]
    assert "<p" not in between and "aria-hidden" not in between  # no "ou" any more
    counter = section.find('class="counter"')
    if data.method == "single_choice":
        assert counter == -1
    else:
        assert blank < counter
        assert 'role="status"' in section[counter : section.index(">", counter)]


@pytest.mark.parametrize("data", [RESOLUTION, APPROVAL, MAJORITY_JUDGMENT, CONDORCET], ids=str)
def test_the_summary_names_the_blank_with_the_words_of_the_ballot(
    voter: TestClient, world: World, owner: User, session_id: str, data: PollInput
) -> None:
    """FR-43: the blank choice keeps its label from the ballot to the summary."""
    poll_id = world.poll(owner, session_id, data)
    page = voter.get(f"/{session_id}").text
    row = re.search(r"<label [^>]*>(?:(?!</label>).)*data-blank.*?</label>", page, re.S)
    assert row is not None
    label = re.sub(r"<[^>]+>", "", row[0]).strip()
    assert label == ("Abstention" if data is RESOLUTION else "Vote blanc")
    blank = {"choice": "00"} if data is RESOLUTION else {"blank": "1"}
    summary = review(voter, session_id, poll_id, blank, headers=FETCH).text
    assert f'<p class="summary-main">{label}</p>' in summary
    assert summary.count(label) == 1 and "bulletin blanc" not in summary


def test_the_approval_counter_is_written_in_what_remains(
    voter: TestClient, world: World, owner: User, session_id: str
) -> None:
    """R9: the texts of live.js, in data- attributes; each box is described by the counter, so
    a box blocked at the maximum says why. Empty without JavaScript, and the same whatever
    the choice."""
    poll_id = world.poll(owner, session_id, APPROVAL)
    page = voter.get(f"/{session_id}").text
    counter = re.search(r'<p class="counter" id="poll-(\d+)-counter" role="status"[^>]*>', page)
    assert counter is not None and counter[1] == str(poll_id)
    assert counter[0].endswith(" hidden>") and page[counter.end() :].startswith("</p>")
    for name, text in {
        "data-max": "2",
        "data-left-one": "Encore {n} choix possible.",
        "data-left-other": "Encore {n} choix possibles.",
        "data-full": "Maximum atteint\u00a0: 2 choix sur 2.",
        "data-help": "Décochez une option pour en choisir une autre.",
        "data-over-one": "Trop de choix\u00a0: décochez-en {n}.",
    }.items():
        assert f'{name}="{text}"' in counter[0], name
    described = f'aria-describedby="poll-{poll_id}-counter"'
    assert page.count(described) == len(APPROVAL.options)
    assert "aria-disabled" not in page  # live.js blocks, the server refuses (FR-44)
    sent_back = review(voter, session_id, poll_id, {"approved": ["1", "2"], "action": "edit"})
    assert counter[0] in sent_back.text  # the same for every choice

    free = world.poll(owner, session_id, PollInput("Commissions", "approval", ["A", "B"]))
    page = voter.get(f"/{session_id}").text
    counter = re.search(rf'<p class="counter" id="poll-{free}-counter"[^>]*>', page)
    assert counter is not None and "data-max" not in counter[0]
    assert 'data-count-one="{n} option cochée."' in counter[0]
    assert 'data-count-other="{n} options cochées."' in counter[0]


def test_the_summary_is_flagged_and_shows_the_default_grade_dashed(
    voter: TestClient, world: World, owner: User, session_id: str
) -> None:
    """R11, FR-43: "Bulletin pas encore déposé", the steps with "Vérifier" current, and a grade
    given by default drawn apart from a grade chosen."""
    poll_id = world.poll(owner, session_id, MAJORITY_JUDGMENT)
    for headers in ({}, FETCH):
        summary = review(voter, session_id, poll_id, {"grade-02": "1"}, headers=headers).text
        flag = summary.index('<p class="pending-flag">Bulletin pas encore déposé</p>')
        assert summary.index("<h2", summary.index(f'id="poll-{poll_id}"')) < flag
        assert flag < summary.index("<h3>Vérifiez votre vote</h3>")
        assert steps_of(summary) == [
            ('class="is-done"', "Choisir"),
            ('aria-current="step"', "Vérifier"),
            ("", "Déposé dans l'urne"),
        ]
        assert summary.count('<span class="grade is-default">À rejeter</span>') == 2
        assert '<span class="grade grade-1">Bien</span>' in summary
        assert summary.count("(sans mention)") == 2
    ballot = voter.get(f"/{session_id}").text
    assert "stepper" not in ballot and "pending-flag" not in ballot  # never on a ballot
    graded = review(voter, session_id, poll_id, {f"grade-{n:02d}": "5" for n in (1, 2, 3)}).text
    assert "is-default" not in graded and graded.count('class="grade grade-5"') == 3


def test_the_confirmation_says_it_once_and_leads_to_the_next_vote(
    voter: TestClient, app: FastAPI, world: World, owner: User, session_id: str
) -> None:
    """FR-43: every step done, the heading alone says "Vote enregistré"; "Vote suivant" stays
    empty and hidden until live.js fills it from the summary of the open polls."""
    poll_id = world.poll(owner, session_id, APPROVAL)
    world.poll(owner, session_id, PollInput("Budget de la fête", "single_choice", ["Oui", "Non"]))
    done = cast(voter, session_id, poll_id, {"approved": ["2"]}, headers=FETCH)
    assert done.status_code == 200
    assert steps_of(done.text) == [
        ('class="is-done"', "Choisir"),
        ('class="is-done"', "Vérifier"),
        ('class="is-done"', "Déposé dans l'urne"),
    ]
    assert done.text.count("enregistré") == 1 and 'role="status"' not in done.text
    assert "Votre bulletin est secret et ne peut plus être modifié." in done.text
    link = re.search(
        r'<p class="next-vote" data-next-vote hidden><a ([^>]*)><span></span>', done.text
    )
    assert link is not None and 'data-msg="Vote suivant\u00a0: {title}"' in link[1]
    assert "Budget" not in done.text  # the next poll is never named by the server
    again = cast(voter, session_id, poll_id, {"approved": ["1"]}, headers=FETCH)
    assert again.status_code == 409 and "Vote déjà enregistré." in again.text  # FR-45


def test_ballots_through_the_service_stay_consistent(
    voter: TestClient, world: World, owner: User, session_id: str
) -> None:
    """A vote cast in the page counts in the tally like any other (one ballot per credential)."""
    poll_id = world.poll(owner, session_id)
    vote_through_summary(voter, session_id, poll_id, {"choice": "1"})
    with world.database.write() as db:
        result = polls.close_poll(db, owner, poll_id)
    assert result.data["counts"] == {"cast": 1, "blank": 0, "valid": 1}


@pytest.mark.parametrize("digit", ["²", "٣"])  # str.isdigit() accepts both, int() refuses "²"
@pytest.mark.parametrize(
    ("data", "field"),
    [
        (RESOLUTION, "choice"),
        (APPROVAL, "approved"),
        (MAJORITY_JUDGMENT, "grade-01"),
        (CONDORCET, "rank-01"),
    ],
    ids=["single_choice", "approval", "majority_judgment", "condorcet"],
)
def test_digits_of_other_scripts_are_a_malformed_ballot(
    voter: TestClient,
    world: World,
    owner: User,
    session_id: str,
    data: PollInput,
    field: str,
    digit: str,
) -> None:
    poll_id = world.poll(owner, session_id, data)
    for send in (review, cast):
        for headers in ({}, FETCH):
            response = send(voter, session_id, poll_id, {field: digit}, headers=headers)
            assert response.status_code == 422, response.text
            assert "Ce bulletin n&#39;a pas pu être lu." in response.text
    assert ballots_of(world, poll_id) == []


def test_choice_of_a_condorcet_ballot(world: World, owner: User, session_id: str) -> None:
    """``Choice`` before confirmation: the ranks as typed, best first, the unranked last."""
    from agaura.web.live.ballot_forms import Choice
    from tests.web.live.test_padding import load

    poll = load(world, session_id, world.poll(owner, session_id, CONDORCET))
    choice = Choice("condorcet", {"ranks": {"3": 2, "1": 2, "4": 1}})
    assert [(rank, option.label) for rank, option in choice.by_rank(poll)] == [
        (1, "Fil"),
        (2, "Gazette"),
        (2, "Lien"),
        (None, "Écho"),
    ]
    assert choice.unranked(poll) and choice.rank_of(2) is None
    # Every option, in the poll's order, "00" for the one left unranked: one size (SPEC §9.2).
    assert choice.fields(poll) == [
        ("rank-01", "02"),
        ("rank-02", "00"),
        ("rank-03", "02"),
        ("rank-04", "01"),
    ]
    everything = Choice("condorcet", {"ranks": {"1": 1, "2": 1, "3": 1, "4": 1}})
    assert not everything.unranked(poll)
    assert not Choice("condorcet").unranked(poll) and Choice("condorcet").rank_of(1) is None
    assert not Choice("majority_judgment", {"grades": {"1": 0}}).unranked(poll)
