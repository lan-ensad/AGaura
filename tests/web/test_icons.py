"""Icons (charter v2): the self-hosted sprite, the macro of macros/icons.html, and their use in
the templates. Every name used exists in the sprite; an icon is decorative (aria-hidden) unless
it carries an accessible name (role="img" and aria-label); the sprite holds shapes only."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agaura
from agaura.models import User
from agaura.services import assemblies
from agaura.web.templating import templates
from tests.conftest import World

PACKAGE = Path(agaura.__file__).parent
SPRITE = PACKAGE / "static" / "icons.svg"
TEMPLATES = PACKAGE / "templates"
SVG = "{http://www.w3.org/2000/svg}"

# icon("name" …) and icon_button("name" …), with either quote.
ICON_CALL = re.compile(r"""\bicon(?:_button)?\(\s*(["'])([\w-]+)\1""")
ICON_TAG = re.compile(r"<svg\b[^>]*\bclass=\"icon\b[^\"]*\"[^>]*>", re.DOTALL)
USED_SYMBOL = re.compile(r"/static/icons\.svg\?v=[0-9a-f]+#i-([\w-]+)")


def sprite() -> ElementTree.Element:
    """The sprite's tree: a file of the repository, not untrusted input."""
    return ElementTree.parse(SPRITE).getroot()  # noqa: S314 (a file of the repository)


def symbols() -> set[str]:
    root = sprite()
    ids = [symbol.get("id", "") for symbol in root.iter(f"{SVG}symbol")]
    assert len(ids) == len(set(ids)), "duplicate symbol id"
    assert all(name.startswith("i-") for name in ids)
    return {name.removeprefix("i-") for name in ids}


def template_sources() -> dict[str, str]:
    """Each template, without its Jinja comments (documentation, examples)."""
    return {
        str(path.relative_to(TEMPLATES)): re.sub(
            r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.DOTALL
        )
        for path in sorted(TEMPLATES.rglob("*.html"))
    }


def test_sprite_holds_shapes_only() -> None:
    """No script, no style, no event handler, no link outside the sprite: the file is served
    from the same origin and must stay inert."""
    root = sprite()
    assert root.tag == f"{SVG}svg"
    for element in root.iter():
        tag = element.tag.removeprefix(SVG)
        assert tag not in {"script", "style", "foreignObject", "image", "a", "use"}, tag
        for attribute in element.attrib:
            assert attribute != "style", f"style attribute on {tag}"
            assert not attribute.lower().startswith("on"), f"{attribute} on {tag}"
            assert not attribute.endswith("href"), f"{attribute} on {tag}"
    for symbol in root.iter(f"{SVG}symbol"):
        assert symbol.get("viewBox"), symbol.get("id")
    text = SPRITE.read_text(encoding="utf-8")
    assert "<script" not in text and "style=" not in text
    assert {"agaura", "ship-wheel", "vote", "lock", "timer"} <= symbols()


def test_licence_ships_next_to_the_sprite() -> None:
    licence = (PACKAGE / "static" / "LICENSE-icons.txt").read_text(encoding="utf-8")
    assert "Lucide" in licence and "ISC License" in licence
    assert "Lucide" in SPRITE.read_text(encoding="utf-8").split("\n", 1)[0]


def test_every_icon_named_in_the_templates_exists() -> None:
    known = symbols()
    used: dict[str, set[str]] = {}
    for name, source in template_sources().items():
        for match in ICON_CALL.finditer(source):
            used.setdefault(match[2], set()).add(name)
    unknown = {name: sorted(places) for name, places in used.items() if name not in known}
    assert unknown == {}


VOTER_SPRITE = PACKAGE / "static" / "icons-voter.svg"
VOTER_TEMPLATES = ("live/", "layouts/voter.html")


def symbol_sources(path: Path) -> dict[str, str]:
    """Each <symbol> of a sprite, as written in the file."""
    text = path.read_text(encoding="utf-8")
    found = re.findall(r'(<symbol id="i-([\w-]+)".*?</symbol>)', text, re.DOTALL)
    return {name: source for source, name in found}


def test_voter_sprite_mirrors_the_main_one() -> None:
    """icons-voter.svg keeps the voter pages light (NFR-04): each of its symbols is the very
    symbol of icons.svg, and it ships the same licence line."""
    main, voter = symbol_sources(SPRITE), symbol_sources(VOTER_SPRITE)
    assert voter and set(voter) <= set(main)
    for name, source in voter.items():
        assert source == main[name], name
    text = VOTER_SPRITE.read_text(encoding="utf-8")
    assert "Lucide" in text.split("\n", 1)[0] and "LICENSE-icons.txt" in text.split("\n", 1)[0]
    assert "<script" not in text and "style=" not in text and "href" not in text


def test_voter_templates_use_the_voter_sprite() -> None:
    """The voter templates import voter_icon as icon (a choice of the template, so a padded vote
    step renders the same markup), and every icon they name is in the voter sprite."""
    voter = set(symbol_sources(VOTER_SPRITE))
    for name, source in template_sources().items():
        if not name.startswith(VOTER_TEMPLATES) or "icon(" not in source:
            continue
        assert "import voter_icon as icon with context" in source, name
        assert "import icon" not in source, name
        missing = {match[2] for match in ICON_CALL.finditer(source)} - voter
        assert missing == set(), f"{name}: copy {sorted(missing)} from icons.svg"


def test_no_icon_without_a_role_in_the_templates() -> None:
    """A literal <svg class="icon"> is either hidden from screen readers or named."""
    for name, source in template_sources().items():
        for tag in ICON_TAG.findall(source):
            assert 'aria-hidden="true"' in tag or 'role="img"' in tag, f"{name}: {tag}"


MACROS = templates.env.from_string(
    '{% from "macros/icons.html" import icon, icon_button with context %}'
    "{{ icon(name, label, size) }}|{{ icon_button(name, label or 'x', tip) }}"
)


def render(name: str, label: str | None = None, size: str | None = None, tip: str | None = None):
    rendered = MACROS.render(name=name, label=label, size=size, tip=tip)
    icon, button = rendered.split("|")
    return icon, button


def test_macro_decorative_icon() -> None:
    icon, _ = render("vote")
    assert icon.startswith('<svg class="icon" aria-hidden="true" focusable="false">')
    assert USED_SYMBOL.search(icon)[1] == "vote"  # type: ignore[index]
    assert "role=" not in icon and "aria-label" not in icon
    sized, _ = render("timer", size="md")
    assert sized.startswith('<svg class="icon icon-md" aria-hidden="true"')


def test_macro_named_icon_and_icon_button() -> None:
    icon, button = render("lock", label="Entrées verrouillées", tip="Verrou")
    assert 'role="img" aria-label="Entrées verrouillées"' in icon and "aria-hidden" not in icon
    assert 'class="button small tip" aria-label="Entrées verrouillées" data-tip="Verrou"' in button
    assert '<svg class="icon" aria-hidden="true"' in button  # the button carries the name
    _, untipped = render("x", label="Retirer")
    assert 'data-tip="Retirer"' in untipped
    escaped, _ = render("x", label='"><script>')
    assert "<script>" not in escaped


def assert_icons_sound(html: str) -> None:
    known = symbols()
    for tag in ICON_TAG.findall(html):
        named = 'role="img"' in tag and re.search(r'aria-label="[^"]+"', tag)
        assert 'aria-hidden="true"' in tag or named, tag
    assert set(USED_SYMBOL.findall(html)) <= known


@pytest.mark.parametrize("state", ["preparation", "in_session", "closed"])
def test_rendered_console_icons(admin: TestClient, world: World, owner: User, state: str) -> None:
    """Names passed through variables too: every icon of the rendered page is in the sprite."""
    public_id = world.assembly(owner, in_session=state != "preparation")
    if state == "closed":
        with world.database.write() as db:
            assemblies.close_assembly(db, owner, public_id)
    html = admin.get(f"/manage/{public_id}").text
    assert_icons_sound(html)
    assert_icons_sound(admin.get("/dashboard").text)
