"""Colour tokens of app.css: WCAG 2.x contrast of the pairs the interface relies on, and the
grade colours written again in the results macro (SVG fills, PDF minutes)."""

import re
from pathlib import Path

import pytest

import agaura
from agaura.web.templating import templates

APP_CSS = Path(agaura.__file__).parent / "static" / "css" / "app.css"
WHITE = "#ffffff"


def tokens() -> dict[str, str]:
    """Custom properties of the first :root block whose value is a hex colour."""
    root = re.search(r":root\s*\{(.*?)\n\}", APP_CSS.read_text(encoding="utf-8"), re.DOTALL)
    assert root is not None
    return {
        name: value.lower()
        for name, value in re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", root.group(1))
    }


def luminance(colour: str) -> float:
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first: str, second: str) -> float:
    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


TEXT = 4.5  # WCAG 1.4.3, text at body size
UI = 3.0  # WCAG 1.4.11, boundary of a control

# (foreground, background, minimum): a token name, or WHITE.
PAIRS = [
    ("text", "bg", TEXT),
    ("text", "surface", TEXT),
    ("muted", "surface", TEXT),
    ("muted", "bg", TEXT),
    ("muted", "accent-soft", TEXT),
    ("border-strong", "surface", UI),
    ("border-strong", "bg", UI),
    ("border-strong", "accent-soft", UI),
    (WHITE, "accent", TEXT),
    ("accent", "accent-soft", TEXT),
    ("accent", "bg", TEXT),
    ("accent-strong", "accent-soft", TEXT),
    ("success", "success-soft", TEXT),
    (WHITE, "success", TEXT),
    ("warning", "warning-soft", TEXT),
    ("warning", "surface", TEXT),
    ("danger", "danger-soft", TEXT),
    (WHITE, "danger", TEXT),
    ("neutral-ink", "neutral-soft", TEXT),
    # Charter v2: text of the tinted notices, the plum of what runs now, the night blue band.
    ("success-ink", "success-soft", TEXT),
    ("warning-ink", "warning-soft", TEXT),
    ("danger-ink", "danger-soft", TEXT),
    ("live", "surface", TEXT),
    ("live", "bg", TEXT),
    ("live", "live-soft", TEXT),
    (WHITE, "live", TEXT),
    (WHITE, "night", TEXT),
    ("night-ink", "night", TEXT),
    (WHITE, "grade-0", TEXT),
    (WHITE, "grade-1", TEXT),
    ("text", "grade-2", TEXT),
    ("text", "grade-3", TEXT),
    ("text", "grade-4", TEXT),
    (WHITE, "grade-5", TEXT),
]


@pytest.mark.parametrize(("foreground", "background", "minimum"), PAIRS)
def test_token_contrast(foreground: str, background: str, minimum: float) -> None:
    colours = tokens() | {WHITE: WHITE}
    ratio = contrast(colours[foreground], colours[background])
    assert ratio >= minimum, f"{foreground} on {background}: {ratio:.2f}:1"


def test_neighbouring_grades_stay_apart() -> None:
    """Grades are always written out; colours of neighbours still differ."""
    colours = tokens()
    for grade in range(5):
        assert contrast(colours[f"grade-{grade}"], colours[f"grade-{grade + 1}"]) > 1.1


GRADE_COLOR = templates.env.from_string(
    '{% from "macros/results.html" import grade_color %}{{ grade_color(grade, count) }}'
)


def test_merit_profile_fills_follow_the_grade_tokens() -> None:
    colours = tokens()
    for grade in range(6):
        assert GRADE_COLOR.render(grade=grade, count=6).strip() == colours[f"grade-{grade}"]
    for count in (2, 3, 5, 7, 10):
        assert GRADE_COLOR.render(grade=0, count=count).strip() == colours["grade-0"]
        assert GRADE_COLOR.render(grade=count - 1, count=count).strip() == colours["grade-5"]
