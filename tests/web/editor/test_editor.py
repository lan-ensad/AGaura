"""Poll creation "the method first" (D-C), draft editing, draft list and copy from a past session
(FR-15, FR-30 to FR-33)."""

from __future__ import annotations

import html
import re
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agaura.models import AccessMode, PollStatus, User
from agaura.services import assemblies, polls
from agaura.services.polls import OptionInput, PollInput
from agaura.web.manage.editor import (
    EditorFields,
    fields_from_kind,
    fields_from_poll,
    format_percent,
    parse_percent,
    rule_sentence,
    switch_kind,
)
from tests.conftest import RESOLUTION, World
from tests.web.conftest import log_in, make_account, post
from tests.web.editor.conftest import polls_of
from tests.web.manage.test_delegates import make_link, open_link
from tests.web.test_icons import assert_icons_sound

CHOOSE = PollInput("Lieu", "single_choice", ["Salle", "Parc", "Ferme"])
KINDS = ("resolution", "single_choice", "approval", "majority_judgment", "condorcet")

PRESETS = {
    "majority": {"fraction": "1/2", "strict": True},
    "two_thirds": {"fraction": "2/3", "strict": False},
    "three_quarters": {"fraction": "3/4", "strict": False},
}


def same_rule(params: dict[str, Any], options: int) -> dict[str, Any]:
    """Parameters with thresholds spelled one way: a preset and its fraction are the same rule,
    and so is the engine's default for that number of options (§6.2)."""
    params = dict(params)
    threshold = params.get("threshold")
    if "threshold" not in params and ("adopt_option" in params or "base" in params):
        threshold = "majority" if options == 2 else "plurality"
    if isinstance(threshold, str) and threshold in PRESETS:
        params["threshold"] = PRESETS[threshold]
    elif isinstance(threshold, dict):
        params["threshold"] = {"strict": False, **threshold}
    elif threshold is not None:
        params["threshold"] = threshold
    return params


def new_url(public_id: str) -> str:
    return f"/manage/{public_id}/polls/new"


def flash_of(client: TestClient, public_id: str) -> str:
    return client.get(f"/manage/{public_id}").text


def defaults(kind: str, **changes: Any) -> dict[str, Any]:
    """Step 2 of a method as the browser sends it, untouched but for ``changes``."""
    fields = fields_from_kind(kind).model_dump(exclude=set(EditorFields.BUTTONS))
    return {**fields, "action": "draft", **changes}


def sentence_of(page: str) -> str:
    found = re.search(r"data-rule-sentence data-rule-texts='[^']*'>([^<]*)</p>", page)
    assert found, "no rule sentence"
    return html.unescape(found[1])


def actions_of(page: str) -> str:
    actions = page[page.index('<div class="editor-actions">') :]
    return actions[: actions.index("</div>")]


class TestStepOne:
    def test_the_methods_first(self, admin: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False)
        page = admin.get(new_url(public_id))
        assert page.status_code == 200
        links = re.findall(r'<h3><a href="([^"]*)"[^>]*>([^<]*)</a></h3>', page.text)
        # Four cards: "Choix unique" covers resolutions, and opens on one (§4.4).
        assert links == [
            (f"{new_url(public_id)}?kind=resolution", "Choix unique"),
            (f"{new_url(public_id)}?kind=approval", "Vote par approbation"),
            (f"{new_url(public_id)}?kind=majority_judgment", "Jugement majoritaire"),
            (f"{new_url(public_id)}?kind=condorcet", "Méthode Condorcet"),
        ]
        assert page.text.count('<div class="method-card">') == 4
        assert "Une seule option, résolutions comprises (Pour, Contre, Abstention)" in page.text
        # A list of groups: the voting methods for now.
        assert page.text.count('class="method-cards"') == 1
        assert 'aria-label="Modes de scrutin" data-group="vote"' in page.text
        # Every method on the same level: none set apart, flagged or preselected.
        assert "Le plus courant" not in page.text and "is-common" not in page.text
        assert "checked" not in page.text
        assert "Exemple\u00a0: <q>Approuver le rapport moral</q>" in page.text
        assert re.search(r'<li aria-current="step">Mode de scrutin</li>', page.text)
        assert 'name="option_label"' not in page.text and "editor.js" not in page.text
        assert_icons_sound(page.text)

    def test_an_unknown_method_gives_step_one(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        public_id = world.assembly(owner)
        page = admin.get(new_url(public_id), params={"kind": "borda"}).text
        assert "Choisir le mode de scrutin" in page and 'name="option_label"' not in page

    def test_closed_session(self, admin: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, public_id)
        for params in ({}, {"kind": "resolution"}):
            response = admin.get(new_url(public_id), params=params)
            assert response.status_code == 303
        assert "La session est terminée." in admin.get(response.headers["location"]).text
        assert f'href="{new_url(public_id)}"' not in flash_of(admin, public_id)


class TestStepTwo:
    @pytest.mark.parametrize(
        ("kind", "present", "absent", "sentence"),
        [
            (
                "resolution",
                ['name="threshold"', 'name="base"', "Modifier les intitulés", 'value="Pour"'],
                ['name="seats"', 'name="max_choices"', "Ajouter des options", "Très bien"],
                "Adoptée si «\u00a0Pour\u00a0» obtient plus de la moitié des suffrages exprimés.",
            ),
            (
                "single_choice",
                ['name="threshold"', "Ajouter des options", "Passer au jugement majoritaire"],
                ['name="seats"', 'name="max_choices"', 'name="adopt_option"', "Très bien"],
                "L'option qui obtient le plus de voix l'emporte, si elle est seule en tête.",
            ),
            (
                "approval",
                ['name="seats"', 'name="max_choices"', "Sièges et candidat·es", "Candidat·e 1"],
                ['name="threshold"', 'name="base"', 'name="adopt_option"', "Très bien"],
                "Le ou la candidat·e le plus coché·e est élu·e. "
                "Chaque votant coche un nom au plus.",
            ),
            (
                "majority_judgment",
                ["Très bien", "À rejeter", "Rien à régler"],
                ['name="threshold"', 'name="seats"', "Modifier la règle", 'name="adopt_option"'],
                "L'option dont la mention majoritaire est la meilleure l'emporte.",
            ),
            (
                "condorcet",
                ["Options à classer", "du «\u00a01er choix\u00a0» au dernier", "Rien à régler"],
                ['name="threshold"', 'name="seats"', "Modifier la règle", "Très bien"],
                "L'option préférée à chacune des autres, en duel, l'emporte\u00a0; "
                "un cycle se règle par les chaînes de duels gagnés les plus fortes (Schulze).",
            ),
        ],
    )
    def test_only_the_settings_of_the_method(
        self,
        admin: TestClient,
        world: World,
        owner: User,
        kind: str,
        present: list[str],
        absent: list[str],
        sentence: str,
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        page = admin.get(new_url(public_id), params={"kind": kind}).text
        assert f'data-kind="{kind}"' in page and f'name="kind" value="{kind}"' in page
        assert all(text in page for text in present), [t for t in present if t not in page]
        assert not [text for text in absent if text in page]
        assert sentence_of(page) == sentence
        assert 'name="change" value="1"' in page and "Changer de mode" in page
        assert re.search(r'<li class="is-done">Mode de scrutin</li>', page)
        assert re.search(r'<li aria-current="step">Question et réglages</li>', page)
        assert 'name="minutes"' not in page  # the session is not in session yet
        # The blank vote has a fixed label, no field; no hint about the number of options.
        assert 'name="blank_label"' not in page and "De 2 à 30 options" not in page
        # A resolution is a form of "Choix unique", offered at the top of its step 2.
        method = re.search(r'<p class="method-name">.*?</span>([^<]*)</p>', page)
        forms = re.findall(
            r'name="switch" value="(\w+)" class="form-choice"[^>]* aria-pressed="(\w+)"', page
        )
        if kind in ("resolution", "single_choice"):
            assert method and method[1] == "Choix unique"
            assert "Forme du vote" in page
            assert forms == [
                ("resolution", str(kind == "resolution").lower()),
                ("single_choice", str(kind == "single_choice").lower()),
            ]
        else:
            assert method and method[1] != "Choix unique" and forms == []
        assert_icons_sound(page)

    def test_rows_without_javascript(self, admin: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False)
        page = admin.get(new_url(public_id), params={"kind": "single_choice"}).text
        assert page.count('name="option_label"') == 2 + 1  # two rows and the row template
        assert re.findall(r'name="option_label" value="([^"]*)"', page) == ["", "", ""]
        assert 'name="more"' in page
        # A resolution has its two labels, behind "Modifier les intitulés", and no row to add.
        page = admin.get(new_url(public_id), params={"kind": "resolution"}).text
        assert page.count('name="option_label"') == 2 and "option-row-template" not in page
        assert '<details class="rule-edit" id="labels-edit">' in page
        assert '<details class="rule-edit" id="rule-edit">' in page  # folded

    def test_launch_leads_in_session(self, admin: TestClient, world: World, owner: User) -> None:
        """R12: "Lancer le vote" is the one primary button during the session; "Enregistrer le
        brouillon" is secondary, "Annuler", a way out, is quiet. Before, saving leads."""
        public_id = world.assembly(owner)
        page = admin.get(new_url(public_id), params={"kind": "resolution"}).text
        actions = actions_of(page)
        assert re.search(
            r'name="action" value="launch" class="button primary">Lancer le vote<', actions
        )
        assert re.search(
            r'name="action" value="draft" class="button">Enregistrer le brouillon<', actions
        )
        assert (
            f'<a class="button quiet" href="/manage/{public_id}#tab-polls">Annuler</a>' in actions
        )
        assert page.count("button primary") == 1
        assert 'name="minutes"' in page and "Clôture automatique" in page
        # Enter saves a draft: the first submit button of the form, never a launch.
        form = page[page.index('id="poll-editor"') :]
        first = re.search(r'<button type="submit"([^>]*)>', form)
        assert first and 'name="action" value="draft"' in first[1]
        quiet = world.assembly(owner, in_session=False)
        actions = actions_of(admin.get(new_url(quiet), params={"kind": "resolution"}).text)
        assert "Lancer le vote" not in actions
        assert 'value="draft" class="button primary">Enregistrer le brouillon<' in actions

    def test_each_method_has_its_own_defaults(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        """FR-31: never the rule of the last poll, always the method's explicit default."""
        public_id = world.assembly(owner, in_session=False)
        post(
            admin,
            new_url(public_id),
            defaults(
                "single_choice",
                title="Lieu",
                option_label=["A", "B"],
                threshold="two_thirds",
                base="cast",
            ),
        )
        page = admin.get(new_url(public_id), params={"kind": "single_choice"}).text
        assert re.search(r'value="auto" id="threshold-auto" checked', page)
        assert re.search(r'value="valid" id="base-valid" checked', page)
        page = admin.get(new_url(public_id), params={"kind": "resolution"}).text
        assert re.search(r'value="majority" id="threshold-majority" checked', page)
        assert 'value="Pour"' in page and 'value="Contre"' in page
        assert 'name="adopt_option" value="1"' in page
        # Its blank vote, as voters will see it: "Abstention", fixed.
        assert (
            '<li class="is-blank">Abstention <span class="muted small">(vote blanc)</span></li>'
            in page
        )
        page = admin.get(new_url(public_id), params={"kind": "approval"}).text
        assert re.search(r'name="max_choices" value="seats" id="max_choices-seats" checked', page)
        assert re.search(r'name="seats" type="number"[^>]* value="1"', page)


class TestCreate:
    @pytest.mark.parametrize(
        ("changes", "method", "params"),
        [
            ({}, "single_choice", {}),
            ({"adopt_option": "1"}, "single_choice", {}),  # only a resolution adopts
            (
                {"kind": "resolution", "threshold": "majority"},
                "single_choice",
                {"threshold": "majority", "adopt_option": 1},
            ),
            (
                {
                    "kind": "resolution",
                    "threshold": "custom",
                    "threshold_percent": "60",
                    "threshold_strict": "1",
                    "base": "cast",
                },
                "single_choice",
                {
                    "base": "cast",
                    "threshold": {"fraction": "3/5", "strict": True},
                    "adopt_option": 1,
                },
            ),
            (
                {"threshold": "two_thirds", "base": "attendees"},
                "single_choice",
                {"base": "attendees", "threshold": "two_thirds"},
            ),
            (
                {"threshold": "custom", "threshold_percent": "66,67 %"},
                "single_choice",
                {"threshold": {"fraction": "6667/10000", "strict": False}},
            ),
            ({"kind": "approval", "seats": "2"}, "approval", {"seats": 2, "max_choices": 2}),
            ({"kind": "approval", "max_choices": "none"}, "approval", {"seats": 1}),
            (
                {
                    "kind": "approval",
                    "max_choices": "number",
                    "max_choices_number": "3",
                    "min_share_percent": "50",
                    "min_share_strict": "1",
                },
                "approval",
                {"seats": 1, "max_choices": 3, "min_share": {"fraction": "1/2", "strict": True}},
            ),
            ({"kind": "majority_judgment", "threshold": "custom"}, "majority_judgment", {}),
            ({"kind": "condorcet", "seats": "2", "threshold": "custom"}, "condorcet", {}),
        ],
    )
    def test_each_method_and_its_settings(
        self,
        admin: TestClient,
        world: World,
        owner: User,
        form: dict[str, Any],
        changes: dict[str, str],
        method: str,
        params: dict[str, Any],
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        form["option_label"][2] = "Parc"
        response = post(admin, new_url(public_id), {**form, **changes})
        assert response.status_code == 303
        assert response.headers["location"] == f"/manage/{public_id}#drafts"
        (poll,) = polls_of(world)
        assert (poll.method, poll.params, poll.status) == (method, params, PollStatus.DRAFT)
        assert [(o.label, o.description) for o in poll.options] == [
            ("Salle des fêtes", "Centre-ville"),
            ("Médiathèque", None),
            ("Parc", None),
        ]
        # The blank vote: "Abstention" for a resolution, "Vote blanc" (none stored) otherwise.
        blank = "Abstention" if changes.get("kind") == "resolution" else None
        assert poll.blank_label == blank and poll.description is None
        assert "Vote créé en brouillon." in flash_of(admin, public_id)

    @pytest.mark.parametrize(
        ("kind", "blank"), [("single_choice", None), ("resolution", "Abstention")]
    )
    def test_the_blank_label_is_fixed(
        self,
        admin: TestClient,
        world: World,
        owner: User,
        form: dict[str, Any],
        kind: str,
        blank: str | None,
    ) -> None:
        """A blank label sent with the form (an old page, a forged form) is ignored."""
        public_id = world.assembly(owner, in_session=False)
        response = post(admin, new_url(public_id), {**form, "kind": kind, "blank_label": "Nul"})
        assert response.status_code == 303
        (poll,) = polls_of(world)
        assert poll.blank_label == blank

    def test_a_resolution_by_its_defaults(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        """§4.4: in session, a card, a title, "Lancer le vote"."""
        public_id = world.assembly(owner)
        data = defaults("resolution", title="Budget 2027", action="launch")
        response = post(admin, new_url(public_id), data)
        assert response.headers["location"] == f"/manage/{public_id}#tab-polls"
        (poll,) = polls_of(world)
        assert (poll.title, poll.method, poll.status) == (
            "Budget 2027",
            "single_choice",
            PollStatus.OPEN,
        )
        assert poll.params == {"threshold": "majority", "adopt_option": 1}
        assert [o.label for o in poll.options] == ["Pour", "Contre"]
        assert poll.blank_label == "Abstention" and poll.closes_at is None
        assert "Vote lancé." in flash_of(admin, public_id)

    def test_create_and_launch_with_a_timer(
        self, admin: TestClient, world: World, owner: User, form: dict[str, Any]
    ) -> None:
        public_id = world.assembly(owner)
        published: list[tuple[str, str, str]] = []
        admin.app.state.hub.publish = lambda channel, event, data="": published.append(  # type: ignore[attr-defined]
            (channel, event, data)
        )
        response = post(admin, new_url(public_id), {**form, "action": "launch", "minutes": "5"})
        assert response.headers["location"] == f"/manage/{public_id}#tab-polls"
        (poll,) = polls_of(world)
        assert poll.status is PollStatus.OPEN and poll.closes_at is not None
        assert (f"live:{public_id}", "update", "polls") in published
        assert (f"manage:{public_id}", "turnout", "console") in published

    def test_a_bad_timer_creates_nothing(
        self, admin: TestClient, world: World, owner: User, form: dict[str, Any]
    ) -> None:
        public_id = world.assembly(owner)
        for minutes, message in (("abc", "Saisissez un nombre entier."), ("5000", "1 440")):
            response = post(
                admin, new_url(public_id), {**form, "action": "launch", "minutes": minutes}
            )
            assert response.status_code == 400 and message in response.text
            assert 'id="error-minutes"' in response.text
        assert polls_of(world) == []
        # A draft leaves the timer aside.
        response = post(admin, new_url(public_id), {**form, "minutes": "abc"})
        assert response.status_code == 303 and polls_of(world)[0].status is PollStatus.DRAFT

    def test_launch_before_the_session_creates_nothing(
        self, admin: TestClient, world: World, owner: User, form: dict[str, Any]
    ) -> None:
        """The poll and its push are one transaction: a refused push leaves no draft."""
        public_id = world.assembly(owner, in_session=False)
        response = post(admin, new_url(public_id), {**form, "action": "launch"})
        assert response.status_code == 400
        assert "La session n&#39;a pas encore commencé." in response.text
        assert polls_of(world) == []

    def test_errors_keep_every_value(
        self, admin: TestClient, world: World, owner: User, form: dict[str, Any]
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        fields = {
            **form,
            "title": "",
            "description": "Contexte <b>gras</b>",
            "option_label": ["Salle", "salle", "", "Gymnase", ""],
            "option_description": ["", "", "Sans intitulé", "Grand", ""],
            "threshold": "custom",
            "threshold_percent": "150",
            "threshold_strict": "1",
            "base": "cast",
        }
        response = post(admin, new_url(public_id), fields)
        assert response.status_code == 400
        page = response.text
        assert "Le vote n'a pas été enregistré" in page
        assert re.search(r'id="error-title">Ce champ est obligatoire', page)
        assert "Deux options portent le même intitulé." in page
        assert re.search(r'id="error-option-3">Donnez un intitulé à cette option', page)
        assert re.search(r'id="error-threshold">Saisissez un pourcentage', page)
        # The fold of the rule opens on its faulty field.
        assert '<details class="rule-edit" id="rule-edit" open>' in page
        # Values come back as typed (escaped), filled rows first, in order.
        assert "Contexte &lt;b&gt;gras&lt;/b&gt;" in page
        labels = re.findall(r'name="option_label" value="([^"]*)"', page)
        assert labels[:5] == ["Salle", "salle", "", "Gymnase", ""]
        assert re.findall(r'name="option_description" value="([^"]*)"', page)[2:4] == [
            "Sans intitulé",
            "Grand",
        ]
        assert 'name="threshold_percent" value="150"' in page
        assert re.search(r'value="custom" id="threshold-custom" checked', page)
        assert re.search(r'value="cast" id="base-cast" checked', page)
        assert re.search(r'<option value="1" selected>plus de</option>', page)
        assert 'aria-invalid="true" aria-describedby="error-title"' in page
        assert sentence_of(page).startswith("L'option qui obtient plus de … des bulletins")
        assert polls_of(world) == []

    def test_a_faulty_label_opens_the_labels_of_a_resolution(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        data = defaults("resolution", title="Budget", option_label=["Pour", "pour"])
        page = post(admin, new_url(public_id), data).text
        assert '<details class="rule-edit" id="labels-edit" open>' in page
        assert '<details class="rule-edit" id="rule-edit">' in page
        assert "Deux options portent le même intitulé." in page

    @pytest.mark.parametrize(
        ("changes", "field", "message"),
        [
            ({"kind": "approval", "seats": "4"}, "seats", "Le nombre de sièges"),
            ({"kind": "approval", "seats": "zéro"}, "seats", "Le nombre de sièges"),
            (
                {"kind": "approval", "max_choices": "number", "max_choices_number": "0"},
                "max_choices",
                "Saisissez un nombre entier supérieur ou égal à 1.",
            ),
            (
                {"kind": "approval", "min_share_percent": "0"},
                "min_share",
                "Saisissez un pourcentage",
            ),
            (
                {"threshold": "custom", "threshold_percent": "100", "threshold_strict": "1"},
                "threshold",
                "Saisissez un pourcentage",
            ),
            (
                {"threshold": "custom", "threshold_percent": "1e-999999"},
                "threshold",
                "Saisissez un pourcentage",
            ),
            (
                {"kind": "resolution", "adopt_option": "3"},
                "adopt_option",
                "L&#39;option à adopter n&#39;existe pas.",
            ),
            (
                {"kind": "resolution", "adopt_option": "first"},
                "adopt_option",
                "L&#39;option à adopter n&#39;existe pas.",
            ),
            ({"base": "everyone"}, "base", "Un réglage du vote est invalide."),
        ],
    )
    def test_settings_errors(
        self,
        admin: TestClient,
        world: World,
        owner: User,
        form: dict[str, Any],
        changes: dict[str, str],
        field: str,
        message: str,
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        response = post(admin, new_url(public_id), {**form, **changes})
        assert response.status_code == 400
        assert re.search(f'id="error-{field}">{re.escape(message)}', response.text), response.text
        assert polls_of(world) == []

    def test_an_unknown_method_comes_back_to_step_one(
        self, admin: TestClient, world: World, owner: User, form: dict[str, Any]
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        response = post(admin, new_url(public_id), {**form, "kind": "borda"})
        assert response.status_code == 400
        assert "Choisissez un mode de scrutin." in response.text
        assert response.text.count('type="radio" name="switch"') == 4
        assert 'name="switch" value="single_choice"' in response.text
        assert not re.search(r'name="switch"[^>]* checked', response.text)  # none of them
        assert 'type="hidden" name="title" value="Lieu de la prochaine assemblée"' in response.text
        assert polls_of(world) == []

    @pytest.mark.parametrize(
        ("count", "accepted"), [(1, False), (2, True), (30, True), (31, False)]
    )
    def test_option_limits(
        self,
        admin: TestClient,
        world: World,
        owner: User,
        form: dict[str, Any],
        count: int,
        accepted: bool,
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        labels = [f"Option {number}" for number in range(1, count + 1)]
        response = post(
            admin,
            new_url(public_id),
            {**form, "option_label": [*labels, ""], "option_description": [""] * (count + 1)},
        )
        if accepted:
            assert response.status_code == 303
            (poll,) = polls_of(world)
            assert len(poll.options) == count
        else:
            assert response.status_code == 400
            assert re.search(
                r'id="error-options">Un vote doit comporter entre 2 et 30', response.text
            )
            assert response.text.count('name="option_label" value="Option ') == count

    def test_more_rows_without_javascript(
        self, admin: TestClient, world: World, owner: User, form: dict[str, Any]
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        response = post(admin, new_url(public_id), {**form, "title": "", "more": "1"})
        assert response.status_code == 200
        assert "Le vote n'a pas" not in response.text  # no error shown yet
        labels = re.findall(r'name="option_label" value="([^"]*)"', response.text)
        # Four rows sent, five more; the last one is the row template of editor.js.
        assert labels == ["Salle des fêtes", "Médiathèque", *[""] * 7, ""]
        assert polls_of(world) == []

    def test_quota_is_shown_in_the_form(
        self, client: TestClient, world: World, form: dict[str, Any]
    ) -> None:
        owner = make_account(world)
        with world.database.write() as db:
            db.get(User, owner.id).quota_overrides = {"polls_per_assembly": 1}  # type: ignore[union-attr]
        log_in(client)
        public_id = world.assembly(owner, in_session=False)
        world.poll(owner, public_id, open_it=False)
        response = post(client, new_url(public_id), form)
        assert response.status_code == 400
        assert "Cette session a atteint le nombre maximal de votes." in response.text
        assert 'value="Salle des fêtes"' in response.text
        assert len(polls_of(world)) == 1


class TestChangeMethod:
    def test_change_carries_every_value(
        self, admin: TestClient, world: World, owner: User, form: dict[str, Any]
    ) -> None:
        """ "Changer de mode" shows step 1 again as radio cards; nothing is saved, and every value
        typed comes along in hidden fields."""
        public_id = world.assembly(owner, in_session=False)
        response = post(admin, new_url(public_id), {**form, "description": "Texte", "change": "1"})
        assert response.status_code == 200
        page = response.text
        assert re.search(r'<li aria-current="step">Mode de scrutin</li>', page)
        assert re.search(
            r'name="switch" value="single_choice" id="switch-single_choice"[^>]* checked', page
        )
        assert page.count('type="radio" name="switch"') == 4
        hidden = re.findall(r'<input type="hidden" name="([^"]*)" value="([^"]*)">', page)
        assert ("title", "Lieu de la prochaine assemblée") in hidden
        assert ("description", "Texte") in hidden and ("kind", "single_choice") in hidden
        assert [value for name, value in hidden if name == "option_label"] == form["option_label"]
        assert not {"action", "change", "switch", "more"} & {name for name, _ in hidden}
        assert f'action="{new_url(public_id)}"' in page
        assert polls_of(world) == []

    def test_change_from_a_resolution(self, admin: TestClient, world: World, owner: User) -> None:
        """A resolution has no card: "Changer de mode" checks "Choix unique", whose radio
        button names the resolution, so that "Continuer" brings it back as it was."""
        public_id = world.assembly(owner, in_session=False)
        typed = defaults("resolution", title="Budget", threshold="two_thirds")
        page = post(admin, new_url(public_id), {**typed, "change": "1"}).text
        assert re.findall(
            r'name="switch" value="(\w+)" id="switch-(\w+)"[^>]*?( checked)?>', page
        ) == [
            ("resolution", "single_choice", " checked"),
            ("approval", "approval", ""),
            ("majority_judgment", "majority_judgment", ""),
            ("condorcet", "condorcet", ""),
        ]
        page = post(admin, new_url(public_id), {**typed, "switch": "resolution"}).text
        assert 'data-kind="resolution"' in page and 'value="Budget"' in page
        assert re.search(r'value="two_thirds" id="threshold-two_thirds" checked', page)
        # From a method of free options, its options typed: the free options of "Choix unique".
        typed = defaults("approval", title="CA", option_label=["Alice", "Bruno"])
        page = post(admin, new_url(public_id), {**typed, "change": "1"}).text
        assert 'value="single_choice" id="switch-single_choice"' in page
        page = post(admin, new_url(public_id), {**typed, "switch": "single_choice"}).text
        assert 'data-kind="single_choice"' in page
        assert re.findall(r'name="option_label" value="([^"]+)"', page) == ["Alice", "Bruno"]
        assert polls_of(world) == []

    def test_switch_keeps_what_fits(
        self, admin: TestClient, world: World, owner: User, form: dict[str, Any]
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        typed = {**form, "description": "Texte"}
        # Between methods of free options: the options stay.
        page = post(admin, new_url(public_id), {**typed, "switch": "approval"}).text
        assert 'data-kind="approval"' in page and "Sièges à pourvoir" in page
        assert 'value="Lieu de la prochaine assemblée"' in page and ">Texte</textarea>" in page
        assert re.findall(r'name="option_label" value="([^"]+)"', page) == [
            "Salle des fêtes",
            "Médiathèque",
        ]
        # To a resolution: its own labels, the free options kept aside…
        page = post(admin, new_url(public_id), {**typed, "switch": "resolution"}).text
        assert re.findall(r'name="option_label" value="([^"]+)"', page) == ["Pour", "Contre"]
        hidden = re.findall(r'<input type="hidden" name="(kept_\w+)" value="([^"]*)">', page)
        assert hidden == [
            ("kept_label", "Salle des fêtes"),
            ("kept_description", "Centre-ville"),
            ("kept_label", "Médiathèque"),
            ("kept_description", ""),
        ]
        # …and given back with "Options libres".
        back = defaults(
            "resolution",
            title="Lieu",
            kept_label=["Salle des fêtes", "Médiathèque"],
            kept_description=["Centre-ville", ""],
        )
        page = post(admin, new_url(public_id), {**back, "switch": "single_choice"}).text
        assert re.findall(r'name="option_label" value="([^"]+)"', page) == [
            "Salle des fêtes",
            "Médiathèque",
        ]
        assert 'value="Centre-ville"' in page and "kept_label" not in page
        # A resolution typed from scratch: empty rows back, nothing of the resolution.
        resolution = defaults("resolution", title="Budget", description="Texte")
        page = post(admin, new_url(public_id), {**resolution, "switch": "single_choice"}).text
        assert re.findall(r'name="option_label" value="([^"]*)"', page) == ["", "", ""]
        assert 'value="Budget"' in page and ">Texte</textarea>" in page
        assert 'name="adopt_option"' not in page and "kept_label" not in page
        # The same method again: everything as typed, the rule too.
        page = post(
            admin,
            new_url(public_id),
            {**typed, "threshold": "two_thirds", "switch": "single_choice"},
        ).text
        assert re.search(r'value="two_thirds" id="threshold-two_thirds" checked', page)
        assert polls_of(world) == []

    def test_switch_to_majority_judgment(
        self, admin: TestClient, world: World, owner: User, form: dict[str, Any]
    ) -> None:
        """The advice of a single choice: from 3 options, the button keeps them."""
        public_id = world.assembly(owner, in_session=False)
        page = admin.get(new_url(public_id), params={"kind": "single_choice"}).text
        assert 'name="switch" value="majority_judgment"' in page
        form["option_label"][2] = "Parc"
        page = post(admin, new_url(public_id), {**form, "switch": "majority_judgment"}).text
        assert 'data-kind="majority_judgment"' in page
        assert re.findall(r'name="option_label" value="([^"]+)"', page) == [
            "Salle des fêtes",
            "Médiathèque",
            "Parc",
        ]

    def test_switch_to_condorcet_keeps_the_options(
        self, admin: TestClient, world: World, owner: User, form: dict[str, Any]
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        form["option_label"][2] = "Parc"
        page = post(admin, new_url(public_id), {**form, "switch": "condorcet"}).text
        assert 'data-kind="condorcet"' in page and "Options à classer" in page
        assert re.findall(r'name="option_label" value="([^"]+)"', page) == [
            "Salle des fêtes",
            "Médiathèque",
            "Parc",
        ]
        assert polls_of(world) == []

    def test_switch_unit(self) -> None:
        typed = EditorFields(
            kind="approval", title="T", seats="3", option_label=["A", "B"], minutes="4"
        )
        fresh = switch_kind(typed, "majority_judgment")
        assert (fresh.kind, fresh.title, fresh.option_label, fresh.minutes) == (
            "majority_judgment",
            "T",
            ["A", "B"],
            "4",
        )
        assert fresh.seats == "1"  # the default of the new method
        assert switch_kind(typed.model_copy(update={"change": "1"}), "approval").change == ""

    @pytest.mark.parametrize(
        ("source", "target", "threshold"),
        [
            # A threshold chosen, offered by the other form: kept.
            ({"kind": "single_choice", "threshold": "two_thirds"}, "resolution", "two_thirds"),
            (
                {"kind": "resolution", "threshold": "three_quarters"},
                "single_choice",
                "three_quarters",
            ),
            ({"kind": "resolution", "threshold": "custom"}, "single_choice", "custom"),
            # The default of a form, or a threshold the other one does not offer: its default.
            ({"kind": "single_choice", "threshold": "auto"}, "resolution", "majority"),
            ({"kind": "single_choice", "threshold": "plurality"}, "resolution", "majority"),
            ({"kind": "resolution", "threshold": "majority"}, "single_choice", "auto"),
            # Another method: its own defaults.
            ({"kind": "single_choice", "threshold": "two_thirds"}, "approval", "auto"),
        ],
    )
    def test_switch_between_the_forms_of_a_single_choice(
        self, source: dict[str, str], target: str, threshold: str
    ) -> None:
        typed = EditorFields(
            **source, title="T", base="cast", threshold_percent="60", threshold_strict="1"
        )
        fresh = switch_kind(typed, target)
        assert (fresh.kind, fresh.title, fresh.threshold) == (target, "T", threshold)
        if target == "approval":
            assert (fresh.base, fresh.threshold_percent) == ("valid", "")
        else:  # the base and the percentage typed stay between the two forms
            assert (fresh.base, fresh.threshold_percent, fresh.threshold_strict) == (
                "cast",
                "60",
                "1",
            )

    def test_free_options_kept_aside_by_a_resolution(self) -> None:
        typed = EditorFields(
            kind="majority_judgment",
            option_label=["Jardin", "", "Fresque"],
            option_description=["", "", "Mur nord"],
        )
        resolution = switch_kind(typed, "resolution")
        assert resolution.option_label == ["Pour", "Contre"]
        assert resolution.kept() == [("Jardin", ""), ("Fresque", "Mur nord")]
        # Given back to any method of free options, then no longer kept.
        for kind in ("single_choice", "approval", "condorcet"):
            back = switch_kind(resolution, kind)
            assert back.rows() == [("Jardin", ""), ("Fresque", "Mur nord")]
            assert back.kept() == []


@pytest.mark.parametrize(
    ("fields", "sentence"),
    [
        (
            {"kind": "resolution", "option_label": ["Pour", "Contre"], "threshold": "majority"},
            "Adoptée si «\u00a0Pour\u00a0» obtient plus de la moitié des suffrages exprimés.",
        ),
        (
            {
                "kind": "resolution",
                "option_label": ["Oui", "Non"],
                "threshold": "two_thirds",
                "base": "cast",
            },
            "Adoptée si «\u00a0Oui\u00a0» obtient au moins les deux tiers des bulletins déposés.",
        ),
        (
            {
                "kind": "resolution",
                "option_label": ["A", "B", "C"],
                "adopt_option": "2",
                "threshold": "custom",
                "threshold_percent": "60",
                "threshold_strict": "1",
                "base": "attendees",
            },
            "Adoptée si «\u00a0B\u00a0» obtient plus de 60\u00a0% des présents.",
        ),
        (
            {"kind": "resolution", "option_label": ["", "Contre"], "threshold": "majority"},
            "Adoptée si «\u00a0…\u00a0» obtient plus de la moitié des suffrages exprimés.",
        ),
        (
            {"kind": "single_choice", "option_label": ["A", "B"]},
            "L'option qui obtient plus de la moitié des suffrages exprimés l'emporte.",
        ),
        (
            {"kind": "single_choice", "option_label": ["A", "B", "C"]},
            "L'option qui obtient le plus de voix l'emporte, si elle est seule en tête.",
        ),
        (
            {"kind": "single_choice", "threshold": "custom", "threshold_percent": "66,670"},
            "L'option qui obtient au moins … des suffrages exprimés l'emporte.",
        ),
        (
            {"kind": "single_choice", "threshold": "custom", "threshold_percent": "66.5 %"},
            "L'option qui obtient au moins 66,5\u00a0% des suffrages exprimés l'emporte.",
        ),
        (
            {"kind": "approval", "seats": "2"},
            "Les 2 candidat·es les plus coché·es sont élu·es. Chaque votant coche au plus 2 noms.",
        ),
        (
            {"kind": "approval", "seats": "1", "max_choices": "none"},
            "Le ou la candidat·e le plus coché·e est élu·e. "
            "Chaque votant coche autant de noms qu'il le souhaite.",
        ),
        (
            {
                "kind": "approval",
                "seats": "3",
                "max_choices": "number",
                "max_choices_number": "5",
                "min_share_percent": "25",
            },
            "Les 3 candidat·es les plus coché·es sont élu·es. Chaque votant coche au plus 5 noms. "
            "Pour être élu·e, il faut au moins 25\u00a0% des suffrages exprimés.",
        ),
        (
            {"kind": "approval", "seats": ""},
            "Le ou la candidat·e le plus coché·e est élu·e.",
        ),
        (
            {"kind": "majority_judgment"},
            "L'option dont la mention majoritaire est la meilleure l'emporte.",
        ),
        (
            {"kind": "condorcet", "seats": "3"},
            "L'option préférée à chacune des autres, en duel, l'emporte\u00a0; "
            "un cycle se règle par les chaînes de duels gagnés les plus fortes (Schulze).",
        ),
    ],
)
def test_rule_sentence(fields: dict[str, Any], sentence: str) -> None:
    assert rule_sentence(EditorFields(**fields)) == sentence


def test_an_option_label_stays_as_typed_in_the_sentence() -> None:
    fields = EditorFields(
        kind="resolution", option_label=["{n} {base}", "Contre"], adopt_option="1"
    )
    assert rule_sentence(fields).startswith("Adoptée si «\u00a0{n} {base}\u00a0» obtient")


class TestEdit:
    def edit_url(self, public_id: str, poll_id: int) -> str:
        return f"/manage/{public_id}/polls/{poll_id}/edit"

    def test_form_shows_the_draft_and_saves_it(
        self, admin: TestClient, world: World, owner: User, form: dict[str, Any]
    ) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, RESOLUTION, open_it=False)
        page = admin.get(self.edit_url(public_id, poll_id))
        assert page.status_code == 200
        assert (
            "Modifier le brouillon" in page.text and 'value="Approbation des comptes"' in page.text
        )
        # Step 2 of its method: a single choice that adopts "Pour" is a resolution, the form of
        # "Choix unique" pressed.
        assert 'data-kind="resolution"' in page.text and "Changer de mode" in page.text
        assert re.search(r"</span>Choix unique</p>", page.text)
        assert (
            'value="resolution" class="form-choice" id="form-resolution" aria-pressed="true"'
            in (page.text)
        )
        assert re.search(r'value="majority" id="threshold-majority" checked', page.text)
        assert sentence_of(page.text) == (
            "Adoptée si «\u00a0Pour\u00a0» obtient plus de la moitié des suffrages exprimés."
        )
        assert 'name="action" value="launch"' in page.text  # in session
        response = post(admin, self.edit_url(public_id, poll_id), {**form, "title": "Nouveau"})
        assert response.headers["location"] == f"/manage/{public_id}#drafts"
        (poll,) = polls_of(world)
        assert poll.title == "Nouveau" and [o.label for o in poll.options] == [
            "Salle des fêtes",
            "Médiathèque",
        ]
        assert "Brouillon enregistré." in flash_of(admin, public_id)

    def test_launch_from_the_draft(
        self, admin: TestClient, world: World, owner: User, form: dict[str, Any]
    ) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, CHOOSE, open_it=False)
        response = post(
            admin,
            self.edit_url(public_id, poll_id),
            {**form, "title": "Lieu", "action": "launch", "minutes": "2"},
        )
        assert response.headers["location"] == f"/manage/{public_id}#tab-polls"
        (poll,) = polls_of(world)
        assert poll.status is PollStatus.OPEN and poll.closes_at is not None
        assert [o.label for o in poll.options] == ["Salle des fêtes", "Médiathèque"]
        assert "Vote lancé." in flash_of(admin, public_id)

    def test_change_the_method_of_a_draft(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        data = PollInput("Conseil", "single_choice", ["Alice", "Bruno"], description="Deux postes")
        poll_id = world.poll(owner, public_id, data, open_it=False)
        url = self.edit_url(public_id, poll_id)
        typed = fields_from_poll(polls_of(world)[0]).model_dump(exclude=set(EditorFields.BUTTONS))
        page = post(admin, url, {**typed, "change": "1"}).text
        assert "Modifier le brouillon" in page and f'action="{url}"' in page
        assert re.search(r'<li aria-current="step">Mode de scrutin</li>', page)
        page = post(admin, url, {**typed, "switch": "approval"}).text
        assert 'data-kind="approval"' in page and 'value="Conseil"' in page
        assert ">Deux postes</textarea>" in page
        response = post(admin, url, {**typed, "kind": "approval", "seats": "1"})
        assert response.status_code == 303
        (poll,) = polls_of(world)
        assert (poll.method, poll.params) == ("approval", {"seats": 1, "max_choices": 1})
        assert (poll.title, poll.description) == ("Conseil", "Deux postes")

    def test_a_resolution_draft_turned_into_free_options(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        """The forms of "Choix unique" while editing: "Options libres" posts to the draft's
        own address, then saving stores a single choice that adopts nothing."""
        public_id = world.assembly(owner, in_session=False)
        poll_id = world.poll(owner, public_id, RESOLUTION, open_it=False)
        url = self.edit_url(public_id, poll_id)
        typed = fields_from_poll(polls_of(world)[0]).model_dump(exclude=set(EditorFields.BUTTONS))
        page = post(admin, url, {**typed, "switch": "single_choice"}).text
        assert 'data-kind="single_choice"' in page and f'action="{url}"' in page
        assert (
            'value="single_choice" class="form-choice" id="form-single_choice" aria-pressed="true"'
            in page
        )
        assert re.findall(r'name="option_label" value="([^"]*)"', page) == ["", "", ""]
        assert re.search(r'value="auto" id="threshold-auto" checked', page)
        # Saved as the page sends it: the fields of free options, two of them typed.
        shown = switch_kind(EditorFields(**typed), "single_choice")
        sent = shown.model_dump(exclude=set(EditorFields.BUTTONS))
        response = post(admin, url, {**sent, "option_label": ["Salle", "Parc"], "action": "draft"})
        assert response.status_code == 303
        (poll,) = polls_of(world)
        assert (poll.title, poll.method, poll.params) == (RESOLUTION.title, "single_choice", {})
        assert [o.label for o in poll.options] == ["Salle", "Parc"]
        assert poll.blank_label is None

    def test_errors_keep_the_values(
        self, admin: TestClient, world: World, owner: User, form: dict[str, Any]
    ) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, CHOOSE, open_it=False)
        response = post(admin, self.edit_url(public_id, poll_id), {**form, "title": "x" * 201})
        assert response.status_code == 400 and "Ce texte est trop long." in response.text
        assert "Modifier le brouillon" in response.text and "x" * 201 in response.text
        assert polls_of(world)[0].title == "Lieu"
        more = post(admin, self.edit_url(public_id, poll_id), {**form, "more": "1"})
        assert more.status_code == 200 and "Modifier le brouillon" in more.text

    @pytest.mark.parametrize(
        "data",
        [
            RESOLUTION,
            CHOOSE,
            PollInput("T", "single_choice", ["A", "B"], {"threshold": "plurality", "base": "cast"}),
            PollInput("T", "single_choice", ["A", "B"], {"threshold": {"fraction": "2/3"}}),
            PollInput(
                "T", "single_choice", ["A", "B"], {"threshold": {"fraction": "1/2", "strict": True}}
            ),
            PollInput("T", "single_choice", ["A", "B"], {"threshold": {"fraction": "3/5"}}),
            PollInput("T", "single_choice", ["A", "B", "C"], {"adopt_option": 2}),
            PollInput(
                "T", "single_choice", ["A", "B"], {"adopt_option": 1, "threshold": "plurality"}
            ),
            PollInput("T", "approval", ["A", "B", "C"], {"seats": 2, "max_choices": 2}),
            PollInput("T", "approval", ["A", "B", "C"], {"seats": 2}),
            PollInput(
                "T",
                "approval",
                ["A", "B", "C"],
                {"seats": 1, "max_choices": 3, "min_share": {"fraction": "1/4", "strict": False}},
            ),
            PollInput(
                "T", "majority_judgment", [OptionInput("A", "Détails"), "B"], description="Contexte"
            ),
            PollInput("T", "majority_judgment", ["A", "B"], {"grade_count": 4}),
        ],
    )
    def test_saving_unchanged_keeps_the_poll(
        self, admin: TestClient, world: World, owner: User, data: PollInput
    ) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, data, open_it=False)
        (before,) = polls_of(world)
        page = admin.get(self.edit_url(public_id, poll_id)).text
        fields = fields_from_poll(before).model_dump(exclude=set(EditorFields.BUTTONS))
        assert all(f'value="{label}"' in page for label in fields["option_label"])
        response = post(admin, self.edit_url(public_id, poll_id), {**fields, "action": "draft"})
        assert response.status_code == 303, response.text
        (after,) = polls_of(world)
        count = len(before.options)
        assert same_rule(after.params, count) == same_rule(before.params, count)
        assert (after.title, after.method, after.description) == (
            before.title,
            before.method,
            before.description,
        )
        # The blank label is fixed: "Abstention" for a resolution, none otherwise.
        assert after.blank_label == ("Abstention" if "adopt_option" in before.params else None)
        assert [(o.label, o.description) for o in after.options] == [
            (o.label, o.description) for o in before.options
        ]

    @pytest.mark.parametrize(
        ("data", "blank"),
        [
            (PollInput("T", "single_choice", ["A", "B"], blank_label="Ne se prononce pas"), None),
            (PollInput("T", "approval", ["A", "B"], {"seats": 1}, blank_label="Personne"), None),
            (
                PollInput(
                    "T", "single_choice", ["Oui", "Non"], {"adopt_option": 1}, blank_label="NPPV"
                ),
                "Abstention",
            ),
        ],
    )
    def test_an_old_blank_label_gives_way_to_the_fixed_one(
        self, admin: TestClient, world: World, owner: User, data: PollInput, blank: str | None
    ) -> None:
        """A draft of before (copied from a past session, say) with a blank label of its own:
        the editor shows no field for it, and saving gives it the fixed label."""
        public_id = world.assembly(owner, in_session=False)
        poll_id = world.poll(owner, public_id, data, open_it=False)
        page = admin.get(self.edit_url(public_id, poll_id)).text
        assert 'name="blank_label"' not in page and str(data.blank_label) not in page
        fields = fields_from_poll(polls_of(world)[0]).model_dump(exclude=set(EditorFields.BUTTONS))
        response = post(admin, self.edit_url(public_id, poll_id), {**fields, "action": "draft"})
        assert response.status_code == 303
        assert polls_of(world)[0].blank_label == blank

    def test_only_drafts(
        self, admin: TestClient, world: World, owner: User, form: dict[str, Any]
    ) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, CHOOSE)
        response = admin.get(self.edit_url(public_id, poll_id))
        assert response.status_code == 303
        assert "Ce vote a déjà été lancé" in admin.get(response.headers["location"]).text
        response = post(admin, self.edit_url(public_id, poll_id), {**form, "title": "Changé"})
        assert response.status_code == 303 and polls_of(world)[0].title == "Lieu"
        assert "Ce vote a déjà été lancé" in flash_of(admin, public_id)

    def test_pushed_while_editing(
        self, admin: TestClient, world: World, owner: User, form: dict[str, Any], monkeypatch: Any
    ) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, CHOOSE, open_it=False)
        from agaura.web.manage import editor

        monkeypatch.setattr(editor, "_editable", lambda poll: None)  # the check passed, then…
        with world.database.write() as db:
            polls.open_poll(db, owner, poll_id)  # …another device pushed the poll
        response = post(admin, self.edit_url(public_id, poll_id), {**form, "title": "Changé"})
        assert response.status_code == 303 and polls_of(world)[0].title == "Lieu"
        assert "Ce vote a déjà été lancé" in flash_of(admin, public_id)

    def test_closed_session(self, admin: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, CHOOSE, open_it=False)
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, public_id)
        response = admin.get(self.edit_url(public_id, poll_id))
        assert response.status_code == 303
        assert "La session est terminée." in flash_of(admin, public_id)

    def test_poll_of_another_session_is_not_found(
        self, admin: TestClient, world: World, owner: User, form: dict[str, Any]
    ) -> None:
        first = world.assembly(owner)
        second = world.assembly(owner)
        poll_id = world.poll(owner, second, CHOOSE, open_it=False)
        assert admin.get(self.edit_url(first, poll_id)).status_code == 404
        assert post(admin, self.edit_url(first, poll_id), form).status_code == 404
        assert (
            post(admin, self.edit_url(first, poll_id), {**form, "change": "1"}).status_code == 404
        )
        assert polls_of(world)[0].title == "Lieu"


class TestDraftList:
    def test_links_and_buttons(self, admin: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner)
        first = world.poll(owner, public_id, CHOOSE, open_it=False)
        world.poll(owner, public_id, RESOLUTION, open_it=False)
        page = admin.get(f"/manage/{public_id}").text
        assert f'href="/manage/{public_id}/polls/{first}/edit"' in page
        # "Nouveau vote" leads to the creation page, from Polls and from Steering.
        assert page.count(f'href="{new_url(public_id)}"') == 2
        assert "tous les réglages" not in page and 'class="templates"' not in page
        assert f'action="/manage/{public_id}/polls"' not in page  # the quick forms are gone
        up = re.findall(r'aria-label="Monter\u00a0: ([^"]*)"( disabled)?', page)
        down = re.findall(r'aria-label="Descendre\u00a0: ([^"]*)"( disabled)?', page)
        assert up == [("Lieu", " disabled"), ("Approbation des comptes", "")]
        assert down == [("Lieu", ""), ("Approbation des comptes", " disabled")]
        # Each draft names its method, with its icon: a resolution says so.
        drafts = page[page.index('id="drafts"') :]
        assert re.search(r"#i-circle-dot\"></use></svg> Choix unique · Salle, Parc, Ferme", drafts)
        assert re.search(r"#i-file-check\"></use></svg> Résolution · Pour, Contre", drafts)
        # Each draft can be pushed from its card in Polls; the first one also from the "Next
        # poll" command of Steering.
        assert drafts.count("Lancer le vote") == 2
        assert page.count("Lancer le vote") == 3
        assert f'action="/manage/{public_id}/polls/{first}/open#tab-cockpit"' in page

    def test_duplicate_and_delete_come_back_to_the_drafts(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        public_id = world.assembly(owner, in_session=False)
        poll_id = world.poll(owner, public_id, CHOOSE, open_it=False)
        base = f"/manage/{public_id}/polls/{poll_id}"
        for action in ("duplicate", "delete"):
            response = post(admin, f"{base}/{action}")
            assert response.headers["location"] == f"/manage/{public_id}#drafts"

    def test_move(self, admin: TestClient, world: World, owner: User) -> None:
        public_id = world.assembly(owner, in_session=False)
        first, second, third = (world.poll(owner, public_id, open_it=False) for _ in range(3))
        base = f"/manage/{public_id}/polls"
        response = post(admin, f"{base}/{third}/move", {"direction": "up"})
        assert response.headers["location"] == f"/manage/{public_id}#draft-{third}"
        post(admin, f"{base}/{first}/move", {"direction": "up"})  # already first
        post(admin, f"{base}/{first}/move", {"direction": "down"})
        with world.database.read() as db:
            order = [poll.id for poll in polls.list_polls(db, owner, public_id)]
        assert order == [third, first, second]
        assert "Ordre des brouillons modifié." in flash_of(admin, public_id)
        post(admin, f"{base}/{first}/move", {"direction": "left"})
        assert "ordre des brouillons a changé entre-temps" in flash_of(admin, public_id)

    def test_closed_session_keeps_only_delete(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        public_id = world.assembly(owner)
        poll_id = world.poll(owner, public_id, CHOOSE, open_it=False)
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, public_id)
        page = admin.get(f"/manage/{public_id}").text
        assert f"/polls/{poll_id}/delete" in page
        assert f"/polls/{poll_id}/edit" not in page and f"/polls/{poll_id}/move" not in page
        assert f'href="{new_url(public_id)}"' not in page


class TestCopy:
    def test_step_one_lists_the_other_sessions(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        past = world.assembly(owner, title="AG 2025")
        world.poll(owner, past)
        world.poll(owner, past, CHOOSE)
        target = world.assembly(owner, in_session=False, title="AG 2026")
        page = admin.get(new_url(target)).text
        assert "Reprendre les votes d'une session passée" in page
        assert re.search(rf'<option value="{past}">AG 2025 · 2 votes</option>', page)
        assert f'<option value="{target}">' not in page
        assert "Reprendre les votes" not in admin.get(new_url(past)).text  # nothing to copy
        # Step 1 only, and not in the console any more.
        assert (
            "Reprendre les votes"
            not in admin.get(new_url(target), params={"kind": "approval"}).text
        )
        assert 'id="copy-source"' not in admin.get(f"/manage/{target}").text

    def test_a_delegate_copies_nothing(
        self, app: FastAPI, admin: TestClient, world: World, owner: User
    ) -> None:
        """A delegate link reaches one session and nothing else of the account."""
        past = world.assembly(owner, title="AG 2025")
        world.poll(owner, past)
        target = world.assembly(owner, in_session=False)
        _, token = make_link(world, owner, target, ["polls"])
        browser = open_link(app, target, token)
        page = browser.get(new_url(target))
        assert page.status_code == 200 and "Choisir le mode de scrutin" in page.text
        assert "Reprendre les votes" not in page.text and "AG 2025" not in page.text

    def test_copy(self, admin: TestClient, world: World, owner: User) -> None:
        past = world.assembly(owner)
        world.poll(owner, past)
        world.poll(owner, past, CHOOSE, open_it=False)
        target = world.assembly(owner, in_session=False)
        response = post(admin, f"/manage/{target}/polls/copy", {"source": past.lower()})
        assert response.headers["location"] == f"/manage/{target}#drafts"
        copies = [poll for poll in polls_of(world) if poll.assembly.public_id == target]
        assert [(poll.title, poll.status) for poll in copies] == [
            (RESOLUTION.title, PollStatus.DRAFT),
            ("Lieu", PollStatus.DRAFT),
        ]
        assert "Votes copiés en brouillons" in flash_of(admin, target)

    def test_another_account_session_is_not_found(
        self, admin: TestClient, world: World, owner: User
    ) -> None:
        intruder = make_account(world, "other@example.org")
        foreign = world.assembly(intruder)
        world.poll(intruder, foreign)
        target = world.assembly(owner, in_session=False)
        for source in (foreign, "ZZZZZ9", ""):
            response = post(admin, f"/manage/{target}/polls/copy", {"source": source})
            assert response.status_code == 404
        assert [poll.assembly.public_id for poll in polls_of(world)] == [foreign]
        assert "Reprendre les votes" not in admin.get(new_url(target)).text

    def test_refusals_come_back_as_messages(self, client: TestClient, world: World) -> None:
        owner = make_account(world)
        with world.database.write() as db:
            db.get(User, owner.id).quota_overrides = {"polls_per_assembly": 1}  # type: ignore[union-attr]
        log_in(client)
        past = world.assembly(owner, mode=AccessMode.CODES)
        world.poll(owner, past)
        world.poll(owner, world.assembly(owner), open_it=False)
        target = world.assembly(owner, in_session=False)
        world.poll(owner, target, open_it=False)
        post(client, f"/manage/{target}/polls/copy", {"source": past})
        assert "Cette session a atteint le nombre maximal de votes." in flash_of(client, target)
        post(client, f"/manage/{target}/polls/copy", {"source": target})
        assert "Choisissez une autre session à copier." in flash_of(client, target)
        assert len(polls_of(world)) == 3


@pytest.mark.parametrize(
    ("text", "share"),
    [("50", "1/2"), ("66,67", "6667/10000"), ("12.5 %", "1/8"), ("100", "1"), ("0,01", "1/10000")],
)
def test_percentages_are_exact(text: str, share: str) -> None:
    from fractions import Fraction

    assert parse_percent(text) == Fraction(share)
    assert parse_percent(format_percent(Fraction(share))) == Fraction(share)


@pytest.mark.parametrize("text", ["", "0", "101", "-5", "abc", "1/2", "50.555", "1e3", "nan"])
def test_invalid_percentages(text: str) -> None:
    from agaura.services.errors import InvalidInputError

    with pytest.raises(InvalidInputError, match="editor_invalid_percent"):
        parse_percent(text)


def test_rounded_display() -> None:
    from fractions import Fraction

    assert format_percent(Fraction(1, 3)) == "33,33" and format_percent(Fraction(1, 2)) == "50"


@pytest.mark.parametrize("kind", KINDS)
def test_kind_of_a_stored_poll(kind: str) -> None:
    """Every method's defaults are a valid poll once titled and given options, and read back
    as the same method."""
    from agaura.web.manage.editor import kind_of, poll_input

    fields = fields_from_kind(kind)
    fields.title = "T"
    if kind != "resolution":
        fields.option_label, fields.option_description = ["A", "B"], ["", ""]
    data, errors = poll_input(fields)
    assert errors == {}

    class Stored:
        method = data.method
        params = data.params

    assert kind_of(Stored()).name == kind  # type: ignore[arg-type]


def test_the_hint_of_a_resolution_follows_its_base(
    admin: TestClient, world: World, owner: User
) -> None:
    """The hint under the rule says what the base counts; editor.js shows the one of the radio
    button checked (data-base-hint)."""
    public_id = world.assembly(owner, in_session=False)
    form = {**defaults("resolution", title="Statuts"), "base": "cast", "change": ""}
    page = post(admin, new_url(public_id), {**form, "switch": "resolution"}).text
    hints = dict(re.findall(r'<p class="hint" data-base-hint="(\w+)"( hidden)?>', page))
    assert hints == {"valid": " hidden", "cast": "", "attendees": " hidden"}
    assert "Bulletins déposés\u00a0: tous les bulletins, votes blancs compris." in page
