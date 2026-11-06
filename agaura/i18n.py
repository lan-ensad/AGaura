"""User interface translations (NFR-09).

Source strings are written in English in the code and templates, wrapped in ``_()`` (or
``N_()`` for module-level constants translated later), and translated in the French gettext
catalogs ``agaura/locales/fr/LC_MESSAGES/*.po``. Every ``.po`` file of the directory is loaded
and merged, so each part of the application can keep its own catalog.

``tests/web/test_i18n.py`` extracts every marked string and fails if one has no French
translation.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from babel.messages.pofile import read_po

LOCALE_DIR = Path(__file__).parent / "locales"
DEFAULT_LOCALE = "fr"


def N_(message: str) -> str:  # noqa: N802 (gettext convention)
    """Mark a string for extraction without translating it yet (module-level constants)."""
    return message


class Translator:
    def __init__(
        self, singular: dict[str, str], plural: dict[str, tuple[str, ...]], locale: str
    ) -> None:
        self._singular = singular
        self._plural = plural
        self.locale = locale

    def gettext(self, message: str) -> str:
        return self._singular.get(message) or message

    def ngettext(self, singular: str, plural: str, n: int) -> str:
        forms = self._plural.get(singular)
        # French: singular for 0 and 1 (plural=(n > 1)); English fallback: singular for 1 only.
        if forms and all(forms):
            return forms[0] if n <= 1 else forms[-1]
        return singular if n == 1 else plural


def catalog_files(locale: str = DEFAULT_LOCALE) -> list[Path]:
    return sorted((LOCALE_DIR / locale / "LC_MESSAGES").glob("*.po"))


@lru_cache
def translator(locale: str = DEFAULT_LOCALE) -> Translator:
    singular: dict[str, str] = {}
    plural: dict[str, tuple[str, ...]] = {}
    for path in catalog_files(locale):
        with path.open("rb") as file:
            catalog = read_po(file, locale=locale)
        for message in catalog:
            if not message.id:
                continue  # header
            if isinstance(message.id, tuple | list):
                plural[message.id[0]] = tuple(message.string or ())
            else:
                singular[message.id] = str(message.string or "")
    return Translator(singular, plural, locale)


def _(message: str) -> str:
    return translator().gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return translator().ngettext(singular, plural, n)


# --- Extraction (developer tooling and tests) ---------------------------------------------

PACKAGE_DIR = Path(__file__).parent
_METHOD_MAP = [("templates/**.html", "jinja2.ext:babel_extract"), ("**.py", "python")]
_OPTIONS = {
    "templates/**.html": {
        "extensions": "jinja2.ext.i18n",
        "trim_blocks": "True",
        "lstrip_blocks": "True",
    }
}


def extract_messages() -> dict[str | tuple[str, str], list[str]]:
    """Every translatable string of the package, with where it is used."""
    from babel.messages.extract import DEFAULT_KEYWORDS, extract_from_dir

    keywords = {**DEFAULT_KEYWORDS, "N_": None}
    found: dict[str | tuple[str, str], list[str]] = {}
    for filename, lineno, message, _comments, _context in extract_from_dir(
        str(PACKAGE_DIR), _METHOD_MAP, options_map=_OPTIONS, keywords=keywords
    ):
        key = tuple(message) if isinstance(message, tuple | list) else message
        if isinstance(key, tuple):
            key = (key[0], key[1])
        found.setdefault(key, []).append(f"{filename}:{lineno}")
    return found


def missing_translations(locale: str = DEFAULT_LOCALE) -> dict[str | tuple[str, str], list[str]]:
    """Extracted strings with no translation in any catalog of ``locale``."""
    translations = translator.__wrapped__(locale)  # uncached: catalogs may have just changed
    missing = {}
    for key, places in extract_messages().items():
        if isinstance(key, tuple):
            forms = translations._plural.get(key[0])
            if not forms or not all(forms):
                missing[key] = places
        elif not translations._singular.get(key):
            missing[key] = places
    return missing
