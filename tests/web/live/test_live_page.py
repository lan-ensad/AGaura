"""The live page shows only open polls and results published to voters (FR-41, NFR-04)."""

from __future__ import annotations

import datetime as dt
import re

from fastapi.testclient import TestClient

from agaura.models import User
from agaura.services import access, assemblies, polls
from agaura.services.polls import PollInput
from agaura.web.live.routes import VOTER_COOKIE
from tests.conftest import RESOLUTION, World
from tests.web.conftest import post
from tests.web.live.conftest import (
    APPROVAL,
    MAJORITY_JUDGMENT,
    assert_outline,
    headings,
    join,
    poll_action,
)


def titled(title: str) -> PollInput:
    return PollInput(title=title, method="single_choice", options=["Oui", "Non"])


def test_only_open_polls_and_results_published_to_voters(
    client: TestClient, world: World, owner: User, session_id: str
) -> None:
    join(client, world, owner, session_id)
    voter = world.voters(owner, session_id, 1)[0]
    world.poll(owner, session_id, titled("Brouillon préparé"), open_it=False)
    first = world.poll(owner, session_id, titled("Premier vote ouvert"))
    second = world.poll(owner, session_id, titled("Second vote ouvert"))
    hidden = {
        "Clos non publié": [("close_poll", {})],
        "Annulé en cours": [("cancel_poll", {"reason": "Erreur de libellé"})],
        "Montré à l'écran seulement": [
            ("close_poll", {}),
            ("show_on_screen", {"shown": True}),
        ],
        "Retiré des écrans": [
            ("close_poll", {}),
            ("publish_result", {"voters": True}),
            ("show_on_screen", {"shown": True}),
            ("dismiss_result", {}),
        ],
        "Publié puis annulé": [
            ("close_poll", {}),
            ("publish_result", {"voters": True}),
            ("cancel_poll", {"reason": "Irrégularité"}),
        ],
    }
    for title, steps in hidden.items():
        poll_id = world.poll(owner, session_id, titled(title))
        for action, kwargs in steps:
            poll_action(world, owner, action, poll_id, **kwargs)
    shown = world.poll(owner, session_id, titled("Résultat pour les votants"))
    world.vote(session_id, voter, shown, {"choice": 1})
    poll_action(world, owner, "close_poll", shown)
    poll_action(world, owner, "publish_result", shown, voters=True)

    page = client.get(f"/{session_id}")
    assert page.status_code == 200
    text = page.text
    # Most recent first within each group, open polls above results (the pile grows on top).
    assert text.index("Second vote ouvert") < text.index("Premier vote ouvert")
    assert text.index("Premier vote ouvert") < text.index("Résultat pour les votants")
    assert f'data-key="poll-{first}-open"' in text and f'data-key="poll-{second}-open"' in text
    assert f'data-key="result-{shown}"' in text and "Bulletins déposés" in text
    for title in ["Brouillon préparé", *hidden]:
        assert title.replace("'", "&#39;") not in text, title
    assert "En attente du prochain vote" not in text


def test_waiting_screen_and_results_without_open_polls(
    client: TestClient, world: World, owner: User, session_id: str
) -> None:
    join(client, world, owner, session_id)
    page = client.get(f"/{session_id}")
    assert 'data-key="waiting"' in page.text and "En attente du prochain vote" in page.text
    assert "Vous avez rejoint la session" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert f'action="/{session_id}/leave"' in page.text and "Quitter" in page.text

    poll_id = world.poll(owner, session_id)
    poll_action(world, owner, "close_poll", poll_id)
    poll_action(world, owner, "publish_result", poll_id, voters=True)
    page = client.get(f"/{session_id}")
    assert f'data-key="result-{poll_id}"' in page.text and 'data-key="waiting"' in page.text


def test_ballots_of_the_three_methods_and_the_timer(
    client: TestClient, world: World, owner: User, session_id: str
) -> None:
    join(client, world, owner, session_id)
    single = world.poll(owner, session_id)
    approval = world.poll(owner, session_id, APPROVAL)
    with world.database.write() as db:
        poll = polls.create_poll(db, owner, session_id, MAJORITY_JUDGMENT)
        polls.open_poll(db, owner, poll.id, minutes=15)
        judgment = poll.id
    text = client.get(f"/{session_id}").text
    assert 'type="radio" name="choice" value="01"' in text and "Abstention" in text
    assert text.count('name="approved"') == 3 and 'data-max="2"' in text
    assert "Cochez au plus 2 options." in text and "Vote blanc" in text
    for position in (1, 2, 3):
        assert text.count(f'name="grade-{position:02d}"') == 6
    for label in ("Très bien", "Assez bien", "Insuffisant", "À rejeter"):
        assert label in text
    assert 'class="grade-pill g0"' in text and 'class="grade-pill g5"' in text
    assert "Clôture à" in text
    for poll_id in (single, approval, judgment):
        assert f'action="/{session_id}/polls/{poll_id}/review"' in text


def test_polls_whose_timer_ran_out_are_hidden(
    client: TestClient, world: World, owner: User, session_id: str
) -> None:
    join(client, world, owner, session_id)
    with world.database.write() as db:
        poll = polls.create_poll(db, owner, session_id, RESOLUTION)
        past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
        polls.open_poll(db, owner, poll.id, minutes=1, now=past)
    assert "En attente du prochain vote" in client.get(f"/{session_id}").text


def test_live_fragment(client: TestClient, world: World, owner: User, session_id: str) -> None:
    assert client.get(f"/{session_id}/live").status_code == 401
    join(client, world, owner, session_id)
    poll_id = world.poll(owner, session_id)
    response = client.get(f"/{session_id}/live")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "<html" not in response.text and "<form" in response.text
    assert re.findall(r'data-key="([^"]+)"', response.text) == [f"poll-{poll_id}-open"]


def test_responses_never_carry_other_voters_data(
    client: TestClient,
    other_client: TestClient,
    world: World,
    owner: User,
    session_id: str,
) -> None:
    with world.database.write() as db:
        access.set_session_password(db, world.keyring, owner, session_id, "Tournesol-42")
    join(client, world, owner, session_id)
    join(other_client, world, owner, session_id)
    poll_id = world.poll(owner, session_id)
    world.vote(session_id, client.cookies[VOTER_COOKIE], poll_id, {"choice": 2})

    mine = client.get(f"/{session_id}").text
    theirs = other_client.get(f"/{session_id}").text
    assert f'data-key="poll-{poll_id}-recorded"' in mine and "vote enregistré" in mine
    assert "Contre" not in mine  # the voted state never repeats the choice
    assert f'data-key="poll-{poll_id}-open"' in theirs and "vote enregistré" not in theirs
    for text in (mine, theirs, client.get(f"/{session_id}/live").text):
        assert client.cookies[VOTER_COOKIE] not in text
        assert other_client.cookies[VOTER_COOKIE] not in text
        assert "Tournesol" not in text
    other_client.cookies.clear()
    assert "Tournesol" not in other_client.get(f"/{session_id}").text


def test_page_weight_and_strict_csp(
    client: TestClient, world: World, owner: User, session_id: str
) -> None:
    join(client, world, owner, session_id)
    world.poll(owner, session_id, MAJORITY_JUDGMENT)
    world.poll(owner, session_id, APPROVAL)
    page = client.get(f"/{session_id}")
    # Icons point into the small voter sprite (href="…/icons-voter.svg?v=…#i-NAME"), fetched once
    # then cached; never into the full sprite of the console.
    assets = {url.split("#")[0] for url in re.findall(r'(?:href|src)="(/static/[^"]+)"', page.text)}
    assert len(assets) == 5  # favicon, app.css, live.css, live.js, icons-voter.svg
    assert any("/icons-voter.svg" in url for url in assets)
    assert "/static/icons.svg" not in page.text
    total = len(page.content) + sum(len(client.get(url).content) for url in assets)
    assert total < 85_000  # NFR-04 (100 kB), with room to spare
    assert not re.search(r"<script(?![^>]*\bsrc=)", page.text)
    assert " style=" not in page.text


def summary_of(html: str) -> str | None:
    found = re.search(r'<nav class="live-summary".*?</nav>', html, re.S)
    return found[0] if found else None


def test_summary_of_open_polls(
    client: TestClient, world: World, owner: User, session_id: str
) -> None:
    """From two open polls on: what remains to vote, with a link to each on the same page."""
    join(client, world, owner, session_id)
    token = client.cookies[VOTER_COOKIE]
    first = world.poll(owner, session_id, titled("Approbation des comptes"))
    assert summary_of(client.get(f"/{session_id}").text) is None  # one poll: nothing to sum up
    second = world.poll(owner, session_id, titled("Budget prévisionnel"))
    third = world.poll(owner, session_id, titled("Élection du bureau"))

    text = client.get(f"/{session_id}").text
    summary = summary_of(text)
    assert summary is not None and 'aria-label="Votes ouverts"' in summary
    assert "À voter\u00a0: 3 · Enregistrés\u00a0: 0" in summary
    # live.js writes it on the link of a poll whose summary waits for its confirmation.
    assert 'data-msg-review="À confirmer · {title}"' in summary
    links = re.findall(r'<a href="#poll-(\d+)">([^<]+)</a>', summary)
    assert links == [  # most recent first, as the polls below
        (str(third), "Élection du bureau"),
        (str(second), "Budget prévisionnel"),
        (str(first), "Approbation des comptes"),
    ]
    assert text.index(summary) < text.index(f'id="poll-{third}"')
    key = re.search(r'data-summary data-key="([^"]+)"', text)

    world.vote(session_id, token, second, {"choice": 1})
    text = client.get(f"/{session_id}").text
    summary = summary_of(text)
    assert summary is not None and "À voter\u00a0: 2 · Enregistrés\u00a0: 1" in summary
    assert f'href="#poll-{second}"' not in summary
    assert re.search(r'data-summary data-key="([^"]+)"', text) != key  # live.js replaces it
    for poll_id in (first, third):
        world.vote(session_id, token, poll_id, {"choice": 2})
    summary = summary_of(client.get(f"/{session_id}").text)
    assert summary is not None and "À voter\u00a0: 0 · Enregistrés\u00a0: 3" in summary
    assert "<a " not in summary
    assert summary in client.get(f"/{session_id}/live").text  # refreshed with the rest


def test_a_recorded_poll_is_one_line(
    client: TestClient, world: World, owner: User, session_id: str
) -> None:
    join(client, world, owner, session_id)
    token = client.cookies[VOTER_COOKIE]
    voted = world.poll(owner, session_id, titled("Approbation des comptes"))
    pending = world.poll(owner, session_id, titled("Budget prévisionnel"))
    world.vote(session_id, token, voted, {"choice": 1})
    text = client.get(f"/{session_id}").text
    line = re.search(
        rf'<section class="poll poll-recorded" id="poll-{voted}".*?</section>', text, re.S
    )
    assert line is not None
    assert f'data-key="poll-{voted}-recorded"' in line[0] and "<form" not in line[0]
    title = re.sub(r"<[^>]+>|\s+", " ", re.search(r"<h2.*?</h2>", line[0], re.S)[0]).split()
    assert " ".join(title) == "✓ Approbation des comptes · vote enregistré"
    assert "Bulletin secret et définitif." in line[0]
    assert text.index(f'id="poll-{pending}"') < text.index(line[0])  # the order of FR-41


def test_results_under_their_own_heading(
    client: TestClient, world: World, owner: User, session_id: str
) -> None:
    join(client, world, owner, session_id)
    results = []
    for title in ("Premier résultat", "Second résultat"):
        poll_id = world.poll(owner, session_id, titled(title))
        poll_action(world, owner, "close_poll", poll_id)
        poll_action(world, owner, "publish_result", poll_id, voters=True)
        results.append(poll_id)
    text = client.get(f"/{session_id}").text
    found = assert_outline(text, "AG de l&#39;association")
    heading = found.index((2, "Résultats publiés"))
    assert found[heading + 1 :][:2] == [(3, "Second résultat"), (3, "Premier résultat")]
    # The next vote appears where the waiting screen is: on top, above the results.
    assert text.index('data-key="waiting"') < text.index('data-key="results-title"')
    open_poll = world.poll(owner, session_id, titled("Vote en cours"))
    text = client.get(f"/{session_id}").text
    assert text.index(f'id="poll-{open_poll}"') < text.index("Résultats publiés")
    assert text.index("Résultats publiés") < text.index(f'data-key="result-{results[1]}"')
    poll_action(world, owner, "dismiss_result", results[0])
    poll_action(world, owner, "dismiss_result", results[1])
    assert "Résultats publiés" not in client.get(f"/{session_id}").text


def test_one_level_one_heading_in_every_state(
    client: TestClient, world: World, owner: User, session_id: str
) -> None:
    """The session title of the top bar is the page's ``h1``; poll titles ``h2``, ballot
    steps ``h3`` (axe-core: page-has-heading-one, heading-order)."""
    title = "AG de l&#39;association"
    join(client, world, owner, session_id)
    token = client.cookies[VOTER_COOKIE]
    found = assert_outline(client.get(f"/{session_id}").text, title)  # waiting
    assert (2, "En attente du prochain vote") in found
    first = world.poll(owner, session_id, titled("Premier vote"))
    second = world.poll(owner, session_id, MAJORITY_JUDGMENT)
    found = assert_outline(client.get(f"/{session_id}").text, title)  # polls
    assert (2, "Premier vote") in found and (2, "Projets de quartier") in found
    for headers in ({}, {"X-Requested-With": "fetch"}):  # review: page or section
        review = post(
            client, f"/{session_id}/polls/{second}/review", {"grade-01": "0"}, headers=headers
        )
        if headers:
            assert [level for level, _ in headings(review.text)] == [2, 3]
        else:
            assert (3, "Vérifiez votre vote") in assert_outline(review.text, title)
    world.vote(session_id, token, first, {"choice": 1})
    found = assert_outline(client.get(f"/{session_id}").text, title)  # recorded
    assert (2, "✓ Premier vote · vote enregistré") in found
    done = post(
        client,
        f"/{session_id}/polls/{second}",
        {"grade-01": "0"},
        headers={"X-Requested-With": "fetch"},
    )
    assert headings(done.text) == [(2, "Projets de quartier"), (3, "Vote enregistré")]
    assert '<h3 class="voted-mark" tabindex="-1" data-focus>' in done.text  # focus lands there
    poll_action(world, owner, "close_poll", second)
    poll_action(world, owner, "publish_result", second, voters=True)
    found = assert_outline(client.get(f"/{session_id}").text, title)  # result
    assert (3, "Projets de quartier") in found


def test_announcements_region(
    client: TestClient, world: World, owner: User, session_id: str
) -> None:
    """In the page from the start, empty: live.js fills it when a poll appears, never on the
    first load."""
    join(client, world, owner, session_id)
    world.poll(owner, session_id, titled("Premier vote"))
    text = client.get(f"/{session_id}").text
    region = re.search(
        r'<div class="visually-hidden" role="status" aria-live="polite" data-announce\s+'
        r'data-msg-new-poll="([^"]*)"></div>',
        text,
    )
    assert region is not None and region[1] == "Nouveau vote ouvert\u00a0: {title}"
    assert text.index(region[0]) < text.index('id="live"')
    assert "data-announce" not in client.get(f"/{session_id}/live").text


def test_connection_banner_in_place(
    client: TestClient, world: World, owner: User, session_id: str
) -> None:
    """FR-42: a status in the page from the start, empty, outside the parts a refresh
    replaces; live.js fills it from its data- attributes when the connection is lost."""
    join(client, world, owner, session_id)
    text = client.get(f"/{session_id}").text
    banner = re.search(
        r'<div class="net-banner" role="status" data-net-banner([^>]*)>(.*?)</div>', text, re.S
    )
    assert banner is not None
    messages = dict(re.findall(r'data-msg-(\w+)="([^"]*)"', banner[1]))
    assert messages == {
        "lost": "Connexion perdue, reconnexion en cours…",
        "help": "Le bulletin entamé reste en place.",
        "back": "Connexion rétablie",
    }
    assert banner[2].endswith("<p data-net-text></p>")  # an icon (aria-hidden), no text
    assert 'aria-hidden="true"' in banner[2]
    assert text.index('id="live"') < text.index(banner[0])
    assert "data-net-banner" not in client.get(f"/{session_id}/live").text


def test_after_the_closing_a_device_that_took_part_keeps_the_published_results(
    client: TestClient, other_client: TestClient, world: World, owner: User, session_id: str
) -> None:
    """FR-36: once the session is closed, a device that joined it still reads the results
    published to voters (not those dismissed), under the closing message; a visitor that never
    joined, or a device that left, gets the closing message alone."""
    join(client, world, owner, session_id)
    published = world.poll(owner, session_id)
    dismissed = world.poll(owner, session_id, PollInput("Lieu", "single_choice", ["A", "B"]))
    for poll_id in (published, dismissed):
        poll_action(world, owner, "close_poll", poll_id)
        poll_action(world, owner, "publish_result", poll_id, voters=True)
    poll_action(world, owner, "dismiss_result", dismissed)
    join(other_client, world, owner, session_id)
    assert post(other_client, f"/{session_id}/leave").status_code == 303
    with world.database.write() as db:
        assemblies.close_assembly(db, owner, session_id)

    for _ in range(2):  # the device keeps its cookie: the results stay
        page = client.get(f"/{session_id}")
        assert page.status_code == 200
        assert "La session est terminée." in page.text
        assert "Résultats publiés" in page.text and "Approbation des comptes" in page.text
        assert "Lieu" not in page.text
        assert client.cookies.get(VOTER_COOKIE)
    for device in (other_client, TestClient(client.app, follow_redirects=False)):
        page = device.get(f"/{session_id}")
        assert "La session est terminée." in page.text
        assert "Résultats publiés" not in page.text and "Approbation des comptes" not in page.text
