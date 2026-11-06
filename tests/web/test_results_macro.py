"""The shared result macro renders every method and outcome."""

import re

import pytest

from agaura.models import Poll, User
from agaura.services import polls
from agaura.services.polls import PollInput
from agaura.web.templating import templates
from tests.conftest import RESOLUTION, World

MACRO = templates.env.from_string(
    '{% from "macros/results.html" import result_block %}{{ result_block(poll, poll.result.data) }}'
)

CASES = [
    (
        RESOLUTION,
        [{"choice": 1}, {"choice": 1}, {"choice": 2}, None],
        ["Adoptée", "Pour", "66,7 %"],
    ),
    (RESOLUTION, [{"choice": 1}, {"choice": 2}], ["Rejetée"]),
    (RESOLUTION, [None], ["Aucun suffrage exprimé"]),
    (
        PollInput("Lieu", "single_choice", ["Salle", "Parc", "Ferme"]),
        [{"choice": 2}, {"choice": 2}, {"choice": 3}],
        ["Résultat\u00a0: Parc"],
    ),
    (
        PollInput("Lieu", "single_choice", ["Salle", "Parc", "Ferme"]),
        [{"choice": 2}, {"choice": 3}],
        ["Égalité entre Parc, Ferme"],
    ),
    (
        PollInput("Conseil", "approval", ["Alice", "Bruno", "Chloé"], {"seats": 2}),
        [{"approved": [1, 2]}, {"approved": [1]}, {"approved": [2, 3]}],
        ["Élus\u00a0: Alice, Bruno", "Approbations"],
    ),
    (
        PollInput("Conseil", "approval", ["Alice", "Bruno", "Chloé"], {"seats": 2}),
        [{"approved": [1, 2, 3]}],
        ["Égalité entre Alice, Bruno, Chloé"],
    ),
    (
        PollInput(
            "Conseil",
            "approval",
            ["Alice", "Bruno"],
            {"seats": 2, "min_share": {"fraction": "1/2"}},
        ),
        [{"approved": [1]}, {"approved": [1]}, {"approved": [2]}],
        ["Des sièges restent à pourvoir", "Élus\u00a0: Alice"],
    ),
    (
        PollInput("Projets", "majority_judgment", ["Jardin", "Fresque"]),
        [{"grades": {"1": 0, "2": 3}}, {"grades": {"1": 1, "2": 2}}],
        ["Résultat\u00a0: Jardin", "Bien", "Mention majoritaire", "grade-1"],
    ),
    (
        PollInput("Nom", "condorcet", ["Gazette", "Écho", "Lien"]),
        [{"ranks": {"2": 1, "1": 2}}, {"ranks": {"2": 1}}, {"ranks": {"1": 1, "3": 2}}],
        [
            "Résultat\u00a0: Écho",
            "Écho gagne tous ses duels (vainqueur de Condorcet).",
            "Duels gagnés",
        ],
    ),
    (
        PollInput("Nom", "condorcet", ["Gazette", "Écho"]),
        [{"ranks": {"1": 1}}, {"ranks": {"2": 1}}],
        ["Égalité entre Gazette, Écho", "Aucune option ne gagne tous ses duels"],
    ),
    (PollInput("Nom", "condorcet", ["Gazette", "Écho"]), [None], ["Aucun suffrage exprimé"]),
]


@pytest.mark.parametrize(("data", "ballots", "expected"), CASES)
def test_result_block(
    world: World, owner: User, data: PollInput, ballots: list, expected: list[str]
) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, data)
    for token, content in zip(world.voters(owner, public_id, len(ballots)), ballots, strict=True):
        world.vote(public_id, token, poll_id, content)
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
    with world.database.read() as db:
        poll = db.get(Poll, poll_id)
        assert poll is not None
        _ = poll.options, poll.result
    html = MACRO.render(poll=poll)
    for text in expected:
        assert text in html


TURNOUT_MACRO = templates.env.from_string(
    '{% from "macros/results.html" import result_block %}'
    "{{ result_block(poll, poll.result.data, show_turnout=true) }}"
)


def closed(world: World, owner: User, data: PollInput, ballots: list, voters: int = 0) -> Poll:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, data)
    tokens = world.voters(owner, public_id, max(voters, len(ballots)))
    for token, content in zip(tokens, ballots, strict=False):
        world.vote(public_id, token, poll_id, content)
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
    with world.database.read() as db:
        poll = db.get(Poll, poll_id)
        assert poll is not None
        _ = poll.options, poll.result
    return poll


def merit_bars(html: str) -> list[str]:
    return re.findall(r'<svg class="merit".*?</svg>', html, flags=re.DOTALL)


def test_merit_profile(world: World, owner: User) -> None:
    """One stacked bar per option, in vote units: exact widths, median at half the votes."""
    data = PollInput("Projets", "majority_judgment", ["Jardin", "Fresque", "Banc"])
    ballots = [
        {"grades": {"1": 0, "2": 3, "3": 0}},
        {"grades": {"1": 1, "2": 2, "3": 1}},
        None,
        {"grades": {"1": 1, "2": 5, "3": 1}},
    ]
    html = MACRO.render(poll=closed(world, owner, data, ballots))
    bars = merit_bars(html)
    assert len(bars) == 3
    for bar in bars:
        assert 'viewBox="0 0 3 1"' in bar and 'x1="1.5"' in bar and 'role="img"' in bar
        assert sum(int(width) for width in re.findall(r'<rect[^>]* width="(\d+)"', bar)) == 3
    # Jardin: 1 "Very good" then 2 "Good"; colours come with a text equivalent.
    assert 'x="0" y="0" width="1" height="1" fill="#14623a"' in bars[0]
    assert 'x="1" y="0" width="2" height="1" fill="#3a8228"' in bars[0]
    assert 'aria-label="Très bien\u00a0: 1, Bien\u00a0: 2, Assez bien\u00a0: 0' in bars[0]
    # Tie reported at the top with competition ranking: 1 =, 1 =, 3.
    main_table = html.split("merit-legend")[0]
    ranks = re.findall(r'<td class="num">(\d+(?: =)?)</td>', main_table)
    assert ranks == ["1 =", "1 =", "3"]
    assert "Égalité entre Jardin, Banc\u00a0: appliquez vos statuts" in html
    assert "Voix par mention" in html and "moitié des voix" in html
    assert "<strong>Bien\u00a0: 2</strong>" in html


def test_merit_profile_without_valid_votes(world: World, owner: User) -> None:
    data = PollInput("Projets", "majority_judgment", ["Jardin", "Fresque"])
    html = MACRO.render(poll=closed(world, owner, data, [None]))
    assert merit_bars(html) == [] and "Aucun suffrage exprimé" in html
    assert html.count("<td>Jardin") == 1 and html.count("<td>Fresque") == 1


def test_custom_grade_scale_colours(world: World, owner: User) -> None:
    data = PollInput("Notes", "majority_judgment", ["A", "B"], {"grade_count": 3})
    html = MACRO.render(poll=closed(world, owner, data, [{"grades": {"1": 0, "2": 2}}]))
    assert 'fill="#14623a"' in html and 'fill="#c0392b"' in html and "Mention 3" in html
    assert "grade-x" in html


def test_print_mode_avoids_unsupported_svg_features(world: World, owner: User) -> None:
    data = PollInput("Projets", "majority_judgment", ["Jardin", "Fresque"])
    poll = closed(world, owner, data, [{"grades": {"1": 0, "2": 3}}, {"grades": {"1": 1}}])
    template = templates.env.from_string(
        '{% from "macros/results.html" import merit_bar %}{{ merit_bar(row, 6, 2, print=true) }}'
    )
    bar = template.render(row=poll.result.data["outcome"]["options"][0])
    assert "vector-effect" not in bar and "stroke" not in bar and 'class="merit-median"' in bar


@pytest.mark.parametrize(
    ("data", "rule"),
    [
        (RESOLUTION, "Requis\u00a0: plus de la moitié des suffrages exprimés."),
        (
            PollInput("Lieu", "single_choice", ["Salle", "Parc", "Ferme"]),
            "Règle\u00a0: l'option qui obtient le plus de voix l'emporte, "
            "si elle est seule en tête.",
        ),
        (
            PollInput("Statuts", "single_choice", ["Pour", "Contre"], {"threshold": "two_thirds"}),
            "Requis\u00a0: au moins les deux tiers des suffrages exprimés.",
        ),
        (
            PollInput(
                "Statuts",
                "single_choice",
                ["Pour", "Contre"],
                {"threshold": "three_quarters", "base": "cast"},
            ),
            "Requis\u00a0: au moins les trois quarts des bulletins déposés.",
        ),
        (
            PollInput(
                "Statuts",
                "single_choice",
                ["Pour", "Contre"],
                {"threshold": {"fraction": "3/5", "strict": True}, "base": "attendees"},
            ),
            "Requis\u00a0: plus de 60,0 % des présents.",
        ),
        (
            PollInput(
                "Statuts", "single_choice", ["Pour", "Contre"], {"threshold": {"fraction": "3/5"}}
            ),
            "Requis\u00a0: au moins 60,0 % des suffrages exprimés.",
        ),
        (
            PollInput("Conseil", "approval", ["A", "B", "C"], {"seats": 2}),
            "2 sièges à pourvoir.",
        ),
        (
            PollInput(
                "Conseil",
                "approval",
                ["A", "B"],
                {"min_share": {"fraction": "1/2", "strict": True}},
            ),
            "Pour être élu·e, il faut plus de 50,0 % des suffrages exprimés.",
        ),
        (
            PollInput("Conseil", "approval", ["A", "B"], {"min_share": {"fraction": "1/3"}}),
            "Pour être élu·e, il faut au moins 33,3 % des suffrages exprimés.",
        ),
    ],
)
def test_rules_are_explained(world: World, owner: User, data: PollInput, rule: str) -> None:
    content = {"choice": 1} if data.method == "single_choice" else {"approved": [1]}
    html = MACRO.render(poll=closed(world, owner, data, [content]))
    assert rule in html


def test_ties_are_marked_on_rows(world: World, owner: User) -> None:
    data = PollInput("Conseil", "approval", ["Alice", "Bruno", "Chloé"], {"seats": 2})
    html = MACRO.render(
        poll=closed(world, owner, data, [{"approved": [1, 2]}, {"approved": [1, 3]}])
    )
    assert html.count('<span class="badge warning">Égalité</span>') == 2
    assert html.count('<span class="badge success">Élu·e</span>') == 1
    assert "1 siège reste à attribuer entre les candidat·es à égalité." in html
    choose = PollInput("Lieu", "single_choice", ["Salle", "Parc", "Ferme"])
    html = MACRO.render(poll=closed(world, owner, choose, [{"choice": 2}, {"choice": 3}]))
    assert html.count('<span class="badge warning">Égalité</span>') == 2


def test_turnout(world: World, owner: User) -> None:
    poll = closed(world, owner, RESOLUTION, [{"choice": 1}, None, {"choice": 2}, {"choice": 1}], 5)
    html = TURNOUT_MACRO.render(poll=poll)
    assert "Participation" in html and "4 / 5" in html and "80,0 % des présents" in html
    assert "Participation" not in MACRO.render(poll=poll)


@pytest.mark.parametrize(
    ("data", "label"),
    [
        (RESOLUTION, "Abstention"),  # the blank label of the poll, as on the ballot
        (PollInput("Lieu", "single_choice", ["Salle", "Parc"]), "Vote blanc"),  # as on the ballot
        (
            PollInput("Lieu", "single_choice", ["Salle", "Parc"], blank_label="Ne se prononce pas"),
            "Ne se prononce pas",
        ),
    ],
)
def test_counts_name_blank_ballots_by_the_blank_label(
    world: World, owner: User, data: PollInput, label: str
) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, data)
    for token in world.voters(owner, public_id, 2):
        world.vote(public_id, token, poll_id, None)
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
    with world.database.read() as db:
        poll = db.get_one(Poll, poll_id)
        _ = poll.options, poll.result
        html = MACRO.render(poll=poll)
    assert f"<dt>{label}</dt><dd><strong>2</strong></dd>" in html
    assert "<dt>Blancs</dt>" not in html


DASH = "\u2013"  # the empty diagonal of a matrix


def duel_rows(html: str, start: int = 0) -> list[list[str]]:
    """Cells of the body rows of the duel table found from ``start``."""
    table = html[html.index('<table class="data duel-table">', start) :]
    body = table[: table.index("</table>")].split("<tbody>")[1]
    return [
        re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
        for row in re.findall(r"<tr>(.*?)</tr>", body, re.S)
    ]


def test_condorcet_duels_in_the_order_of_the_ranking(world: World, owner: User) -> None:
    """The ranking with duels won and lost; the matrix of duels, rows and columns in the order
    of the ranking, the duels won in bold; no matrix of chains with a Condorcet winner."""
    data = PollInput("Nom", "condorcet", ["Gazette", "Écho", "Lien"])
    ballots = [{"ranks": {"3": 1, "2": 2, "1": 3}}] * 2 + [{"ranks": {"2": 1, "1": 2}}]
    html = MACRO.render(poll=closed(world, owner, data, ballots))
    assert "Règle\u00a0: l'option préférée à chacune des autres" in html
    assert "Lien gagne tous ses duels (vainqueur de Condorcet)." in html
    main = html.split("<details")[0]
    assert re.findall(r'<td class="num">(\d+)</td>\s*<td>(?:<strong>)?([^<]+)', main) == [
        ("1", "Lien"),
        ("2", "Écho"),
        ("3", "Gazette"),
    ]
    rows = duel_rows(html)
    assert [row[0] for row in rows] == [
        '<span class="duel-key">1</span> Lien',
        '<span class="duel-key">2</span> Écho',
        '<span class="duel-key">3</span> Gazette',
    ]
    assert rows[0][1:] == [DASH, "<strong>2</strong>", "<strong>2</strong>"]
    assert rows[1][1:] == ["1", DASH, "<strong>3</strong>"]
    assert rows[2][1:] == ["1", "0", DASH]
    assert '<span class="visually-hidden">Lien</span>' in html  # the column heads, read out
    assert "Duels un contre un" in html
    assert "Chaînes de duels gagnés les plus fortes" not in html


def test_condorcet_cycle_shows_the_strongest_chains(world: World, owner: User) -> None:
    data = PollInput("Nom", "condorcet", ["A", "B", "C"])
    orders = [("BAC", 4), ("ACB", 3), ("CBA", 2)]
    ballots = [
        {"ranks": {str(" ABC".index(letter)): rank for rank, letter in enumerate(order, 1)}}
        for order, count in orders
        for _ in range(count)
    ]
    html = MACRO.render(poll=closed(world, owner, data, ballots))
    assert "Résultat\u00a0: B" in html and "Aucune option ne gagne tous ses duels" in html
    assert "Chaînes de duels gagnés les plus fortes" in html
    paths = duel_rows(html, html.index("Chaînes de duels gagnés les plus fortes"))
    assert [row[1:] for row in paths] == [
        [DASH, "<strong>6</strong>", "<strong>6</strong>"],
        ["5", DASH, "<strong>7</strong>"],
        ["5", "5", DASH],
    ]
