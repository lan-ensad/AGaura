"""Results on the projection screen (FR-75): only the closed polls the tribune shows; the verdict
always, the count of each option only from 10 ballots; the latest result in the main place,
earlier ones in one line; ties reported, never broken."""

from __future__ import annotations

import re
from collections.abc import Sequence
from html import unescape
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agaura.models import Poll, PollStatus, User
from agaura.services import assemblies
from agaura.services import screen as screen_service
from agaura.services.polls import PollInput
from tests.conftest import RESOLUTION, World
from tests.web.screen.conftest import ICON, poll_action
from tests.web.screen.test_screen import RESULT_TEXTS, configure, strict_page

CHOOSE = PollInput("Lieu de la prochaine AG", "single_choice", ["Salle", "Parc", "Ferme"])
ELECT = PollInput(
    "Élection du conseil", "approval", ["Alice", "Bruno", "Chloé"], {"seats": 2, "max_choices": 2}
)
GRADE = PollInput("Projets de quartier", "majority_judgment", ["Jardin", "Fresque"])
RANK = PollInput("Nom de la lettre", "condorcet", ["Gazette", "Écho", "Lien"])
TWO_THIRDS = PollInput("Motion", "single_choice", ["Oui", "Non"], {"threshold": "two_thirds"})


def closed(
    world: World,
    owner: User,
    public_id: str,
    data: PollInput,
    ballots: Sequence[dict[str, Any] | None],
    *,
    show: bool = True,
) -> int:
    """A poll voted by new voters, closed, and shown on the screen by default."""
    poll_id = world.poll(owner, public_id, data)
    for token, content in zip(world.voters(owner, public_id, len(ballots)), ballots, strict=True):
        world.vote(public_id, token, poll_id, content)
    poll_action(world, owner, "close_poll", poll_id)
    if show:
        poll_action(world, owner, "show_on_screen", poll_id, shown=True)
    return poll_id


def votes(*counts: tuple[dict[str, Any] | None, int]) -> list[dict[str, Any] | None]:
    return [content for content, count in counts for _ in range(count)]


def card(page: str) -> str:
    """The result card in the main place (exactly one)."""
    cards = re.findall(r'<section class="screen-result".*?</section>', page, re.DOTALL)
    assert len(cards) == 1, page
    return cards[0]


def text_of(page: str) -> str:
    text = " ".join(re.findall(r"[^ \t\n\r\f\v]+", unescape(re.sub(r"<[^>]+>", " ", page))))
    # A tag before a no-break space ("</strong>\u00a0:") leaves no gap in the page.
    return re.sub(r" ?\u00a0 ?", "\u00a0", text)


def closing_time(world: World, poll_id: int) -> str:
    from agaura.web.templating import time_of_day

    with world.database.read() as db:
        return time_of_day(db.get_one(Poll, poll_id).closed_at)


class TestCard:
    def test_resolution_with_the_count_of_each_option(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        ballots = votes(({"choice": 1}, 8), ({"choice": 2}, 2), (None, 2))
        poll_id = closed(world, owner, session_id, RESOLUTION, ballots)
        page = client.get(url).text
        strict_page(page)
        assert 'class="screen-stage hero-result has-side"' in page
        text = text_of(card(page))
        assert (
            "Résultat" in text and f"clos à {closing_time(world, poll_id)} · 12 bulletins" in text
        )
        assert "Approbation des comptes Adoptée" in text
        assert "Requis\u00a0: plus de la moitié des suffrages exprimés." in text
        assert "Pour 8 80,0 % 8 Contre 2 20,0 % 2" in text
        # Blank ballots go by the poll's blank label ("Abstention"), not "Blancs".
        assert "Bulletins déposés 12 Abstention 2 Suffrages exprimés 10" in text
        assert "Blancs" not in page
        assert 'data-verdict="adopted"' in page and '<li class="is-win">' in page
        assert 'max="10" value="8"' in page

    def test_under_ten_ballots_the_verdict_alone(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        ballots = votes(({"choice": 1}, 6), ({"choice": 2}, 2), (None, 1))
        poll_id = closed(world, owner, session_id, RESOLUTION, ballots)
        result = card(client.get(url).text)
        text = text_of(result)
        assert (
            "Adoptée" in text and "Requis\u00a0: plus de la moitié des suffrages exprimés." in text
        )
        notice = "Détail des voix non affiché\u00a0: moins de 10 bulletins"
        assert notice in text
        for absent in ("screen-tally", "screen-counts", "<progress", "merit"):
            assert absent not in result, absent
        for absent in ("Bulletins déposés", "Abstention", "Suffrages exprimés", "%", "Pour 6"):
            assert absent not in text, absent
        # No figure at all but the closing time: not even the number of ballots.
        rest = text.replace(notice, "").replace(closing_time(world, poll_id), "")
        assert not re.search(r"\d", rest), rest

    @pytest.mark.parametrize(
        ("data", "ballots", "kind", "verdict", "lines"),
        [
            (
                RESOLUTION,
                votes(({"choice": 1}, 3), ({"choice": 2}, 7)),
                "rejected",
                "Rejetée",
                ["Pour 3 30,0 %", "Contre 7 70,0 %"],
            ),
            (
                CHOOSE,
                votes(({"choice": 2}, 6), ({"choice": 3}, 3), ({"choice": 1}, 1)),
                "decided",
                "Parc",
                ["Règle\u00a0: l'option qui obtient le plus de voix l'emporte, si elle est seule"],
            ),
            (
                ELECT,
                votes(({"approved": [1, 2]}, 6), ({"approved": [1]}, 3), ({"approved": [3]}, 1)),
                "decided",
                "Élus\u00a0: Alice, Bruno",
                ["2 sièges à pourvoir.", "Alice Élu·e 9 90,0 %", "Bruno Élu·e 6 60,0 %"],
            ),
            (
                GRADE,
                votes(({"grades": {"1": 1, "2": 3}}, 6), ({"grades": {"1": 2, "2": 0}}, 4)),
                "decided",
                "Jardin, mention majoritaire «\u00a0Bien\u00a0»",
                [
                    "Classement par mention majoritaire\u00a0; une option sans mention compte"
                    " «\u00a0À rejeter\u00a0».",
                    "1 Jardin Bien",
                    "2 Fresque Passable",
                    "moitié des voix",
                ],
            ),
            (
                RANK,
                votes(({"ranks": {"2": 1, "3": 2}}, 6), ({"ranks": {"3": 1, "1": 2}}, 4)),
                "decided",
                "Écho",
                [
                    "Classement par duels un contre un (Condorcet, puis Schulze)\u00a0; "
                    "une option non classée passe après les options classées.",
                    "Écho gagne tous ses duels (vainqueur de Condorcet).",
                    "1 Écho 2 duels gagnés",
                    "2 Lien 1 duel gagné",
                    "3 Gazette 0 duel gagné",
                ],
            ),
            (
                TWO_THIRDS,
                votes(({"choice": 1}, 6), ({"choice": 2}, 4)),
                "none",
                "Aucune option n'atteint le seuil requis",
                ["Requis\u00a0: au moins les deux tiers des suffrages exprimés."],
            ),
        ],
    )
    def test_verdicts(
        self,
        client: TestClient,
        world: World,
        owner: User,
        session_id: str,
        url: str,
        data: PollInput,
        ballots: list[dict[str, Any] | None],
        kind: str,
        verdict: str,
        lines: list[str],
    ) -> None:
        closed(world, owner, session_id, data, ballots)
        result = card(client.get(url).text)
        assert f'<p class="screen-verdict is-{kind}" data-verdict="{kind}">' in result
        text = text_of(result)
        assert f"{data.title} {verdict} " in text
        for line in lines:
            assert line in text, line
        blank = data.blank_label or "Vote blanc"
        assert f"Bulletins déposés 10 {blank} 0 Suffrages exprimés 10" in text

    def test_merit_profiles_on_the_night_card(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        ballots = votes(({"grades": {"1": 1, "2": 3}}, 6), ({"grades": {"1": 2, "2": 0}}, 4))
        closed(world, owner, session_id, GRADE, ballots)
        result = card(client.get(url).text)
        assert result.count('<svg class="merit"') == 2
        assert 'stroke="#12306f"' in result and 'class="merit-median"' in result
        profile = "Très bien\u00a0: 0, Bien\u00a0: 6, Assez bien\u00a0: 4"
        assert f'aria-label="{profile}' in result
        assert '<span class="grade grade-1">Bien</span>' in result

    @pytest.mark.parametrize("count", [11, 5])
    def test_a_tie_is_reported_never_broken(
        self,
        client: TestClient,
        world: World,
        owner: User,
        session_id: str,
        url: str,
        count: int,
    ) -> None:
        """With or without the detail: "Égalité" first, the tied options named, no winner."""
        tied = count // 2
        ballots = votes(({"choice": 2}, tied), ({"choice": 3}, tied), ({"choice": 1}, 1))
        closed(world, owner, session_id, CHOOSE, ballots)
        result = card(client.get(url).text)
        text = text_of(result)
        assert (
            f"{CHOOSE.title} Égalité\u00a0: à trancher selon les statuts "
            "À égalité\u00a0: Parc, Ferme." in (text)
        )
        assert 'data-verdict="tie"' in result and "is-win" not in result
        assert "Résultat\u00a0:" not in text and "Adoptée" not in text
        if count >= 10:
            assert result.count('<span class="tag-tie">Égalité</span>') == 2
            assert "Parc Égalité 5 45,5 %" in text and "Ferme Égalité 5 45,5 %" in text
        else:
            assert "tag-tie" not in result and "Détail des voix non affiché" in text

    def test_an_election_tie_names_the_elected_and_the_seats_left(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        ballots = votes(({"approved": [1, 2]}, 5), ({"approved": [1, 3]}, 5))
        closed(world, owner, session_id, ELECT, ballots)
        text = text_of(card(client.get(url).text))
        assert "Égalité\u00a0: à trancher selon les statuts À égalité\u00a0: Bruno, Chloé." in text
        assert "Élus\u00a0: Alice" in text
        assert "1 siège reste à attribuer entre les candidat·es à égalité." in text


class TestPlaces:
    def test_nothing_until_the_tribune_shows_it(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        poll_id = closed(world, owner, session_id, RESOLUTION, [{"choice": 1}] * 10, show=False)
        poll_action(world, owner, "publish_result", poll_id, voters=True)  # voters only
        page = client.get(url).text
        for text in RESULT_TEXTS:
            assert text not in page, text
        poll_action(world, owner, "show_on_screen", poll_id, shown=True)
        assert "Adoptée" in client.get(url).text
        poll_action(world, owner, "show_on_screen", poll_id, shown=False)
        page = client.get(url).text
        for text in RESULT_TEXTS:
            assert text not in page, text
        assert re.search(rf"Approbation des comptes</span>\s*<span[^>]*>{ICON}Vote clos<", page)

    def test_latest_shown_on_the_stage_earlier_ones_in_one_line(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        first = closed(world, owner, session_id, RESOLUTION, [{"choice": 1}] * 10)
        second = closed(world, owner, session_id, ELECT, [{"approved": [1, 2]}] * 10)
        third = closed(world, owner, session_id, CHOOSE, [{"choice": 2}] * 10, show=False)
        page = client.get(url).text
        assert f'data-result="{second}"' in card(page)
        assert f'data-past-poll="{second}"' not in page  # not repeated in the list
        lines = re.findall(
            r'<li class="closed" data-past-poll="(\d+)">\s*<span class="past-title">[^<]*</span>'
            rf'\s*<span class="past-(verdict|state)[^"]*">{ICON}([^<]*)<',
            page,
        )
        assert lines == [
            (str(third), "state", "Vote clos"),
            (str(first), "verdict", "Adoptée"),
        ]
        # Shown again later: it takes the stage back, the other one keeps its line.
        poll_action(world, owner, "show_on_screen", first, shown=False)
        poll_action(world, owner, "show_on_screen", first, shown=True)
        page = client.get(url).text
        assert f'data-result="{first}"' in card(page)
        assert re.search(
            rf'data-past-poll="{second}">\s*<span class="past-title">Élection du conseil</span>'
            r'\s*<span class="past-verdict is-decided">Élus\u00a0: Alice, Bruno<',
            page,
        )
        # Majority Judgment in one line: the winner, without its grade.
        grade = closed(world, owner, session_id, GRADE, [{"grades": {"1": 0, "2": 2}}] * 10)
        poll_action(world, owner, "show_on_screen", first, shown=False)
        poll_action(world, owner, "show_on_screen", first, shown=True)
        page = client.get(url).text
        assert re.search(
            rf'data-past-poll="{grade}">.*?past-verdict is-decided">Jardin<', page, re.S
        )

    def test_next_to_the_polls_open_now(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        poll_id = closed(world, owner, session_id, RESOLUTION, [{"choice": 1}] * 10)
        world.poll(owner, session_id, CHOOSE)
        page = client.get(url).text
        assert 'class="screen-stage hero-questions has-side"' in page
        items = page[page.index('<div class="screen-items columns-2">') :]
        assert items.index("Vote en cours") < items.index('class="screen-result"')
        assert f'data-result="{poll_id}"' in card(page)
        assert "Questions mises aux voix" not in page  # the only past poll is on the card

    def test_after_the_session_a_line_in_the_questions(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        poll_id = closed(world, owner, session_id, RESOLUTION, [{"choice": 1}] * 10)
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, session_id)
        page = client.get(url).text
        assert "La session est terminée." in page and "screen-result" not in page
        assert re.search(
            rf'data-past-poll="{poll_id}">.*?past-verdict is-adopted">Adoptée<', page, re.S
        )

    def test_never_an_open_or_cancelled_poll_whatever_the_flag(
        self, client: TestClient, world: World, owner: User, session_id: str, url: str
    ) -> None:
        """The flag is turned off on cancelling and refused on an open poll; even forced in the
        database, the screen shows no result of an open or cancelled poll (D4, FR-72)."""
        opened = world.poll(owner, session_id, titled_resolution("Vote ouvert"))
        cancelled = closed(world, owner, session_id, RESOLUTION, [{"choice": 1}] * 10)
        poll_action(world, owner, "cancel_poll", cancelled, reason="Irrégularité")
        with world.database.write() as db:
            for poll_id in (opened, cancelled):
                db.get_one(Poll, poll_id).shown_on_screen = True
        page = client.get(url).text
        for text in RESULT_TEXTS:
            assert text not in page, text
        assert "Irrégularité" not in page and "Vote annulé" in page and "Vote en cours" in page


def titled_resolution(title: str) -> PollInput:
    return PollInput(title, "single_choice", ["Pour", "Contre"], {"adopt_option": 1})


class TestLayout:
    def layout(self, world: World, public_id: str) -> tuple[str, list[str]]:
        with world.database.read() as db:
            assembly = assemblies.find_assembly(db, public_id)
            assert assembly is not None
            view = screen_service.screen_view(db, world.keyring, assembly)
            return view.hero.value, [part.value for part in view.side]

    def test_result_after_the_open_polls_before_the_rest(
        self, world: World, owner: User, session_id: str
    ) -> None:
        configure(world, session_id, agenda=[{"title": "Budget"}], timer_shown=True)
        poll_id = closed(world, owner, session_id, RESOLUTION, [{"choice": 1}])
        assert self.layout(world, session_id) == ("result", ["timer", "join", "agenda"])
        opened = world.poll(owner, session_id)
        assert self.layout(world, session_id) == ("questions", ["timer", "join", "agenda"])
        poll_action(world, owner, "close_poll", opened)
        configure(world, session_id, entries_locked=True, timer_shown=False, agenda=[])
        assert self.layout(world, session_id) == ("result", [])
        poll_action(world, owner, "show_on_screen", poll_id, shown=False)
        assert self.layout(world, session_id) == ("questions", [])

    def test_view(self, world: World, owner: User, session_id: str) -> None:
        older = closed(world, owner, session_id, RESOLUTION, [{"choice": 1}] * 9)
        newer = closed(world, owner, session_id, CHOOSE, [{"choice": 1}] * 10)
        with world.database.read() as db:
            assembly = assemblies.find_assembly(db, session_id)
            assert assembly is not None
            view = screen_service.screen_view(db, world.keyring, assembly)
            assert [item.poll.id for item in view.results] == [newer, older]
            assert view.stage_result is not None and view.stage_result.poll.id == newer
            assert [poll.id for poll in view.past_listed] == [older]
            shown = view.result_of(view.past_listed[0])
            assert shown is not None and (shown.ballots, shown.detail) == (9, False)
            assert view.stage_result.detail and view.stage_result.detail_min == 10
            assert view.stage_result.verdict is screen_service.Verdict.DECIDED
            assert all(item.poll.status is PollStatus.CLOSED for item in view.results)
