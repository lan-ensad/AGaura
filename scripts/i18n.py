"""Check or extend the French catalogs.

    uv run python scripts/i18n.py check                          # strings without a translation
    uv run python scripts/i18n.py add manage                     # append them to manage.po
    uv run python scripts/i18n.py add manage web/manage templates/manage   # only from these paths

Appended entries have an empty translation: fill each ``msgstr`` in French, then run ``check``.
"""

from __future__ import annotations

import sys

from agaura.i18n import DEFAULT_LOCALE, LOCALE_DIR, missing_translations

HEADER = """msgid ""
msgstr ""
"Language: fr\\n"
"MIME-Version: 1.0\\n"
"Content-Type: text/plain; charset=UTF-8\\n"
"Content-Transfer-Encoding: 8bit\\n"
"Plural-Forms: nplurals=2; plural=(n > 1);\\n"
"""


def quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def main(argv: list[str]) -> int:
    missing = missing_translations()
    if len(argv) > 2 and argv[0] == "add":  # restrict to strings used under the given paths
        prefixes = tuple(argv[2:])
        missing = {
            key: places
            for key, places in missing.items()
            if any(place.startswith(prefixes) for place in places)
        }
        argv = argv[:2]
    if not argv or argv[0] == "check":
        for key, places in missing.items():
            print(f"{places[0]}: {key!r}")
        print(f"{len(missing)} string(s) without a French translation.")
        return 1 if missing else 0
    if argv[0] == "add" and len(argv) == 2:
        path = LOCALE_DIR / DEFAULT_LOCALE / "LC_MESSAGES" / f"{argv[1]}.po"
        chunks = [] if path.exists() else [HEADER]
        for key, places in missing.items():
            chunks.append(f"\n#: {' '.join(places[:3])}")
            if isinstance(key, tuple):
                chunks.append(f"msgid {quote(key[0])}\nmsgid_plural {quote(key[1])}")
                chunks.append('msgstr[0] ""\nmsgstr[1] ""')
            else:
                chunks.append(f'msgid {quote(key)}\nmsgstr ""')
        with path.open("a", encoding="utf-8") as file:
            file.write("\n".join(chunks) + "\n")
        print(f"{len(missing)} string(s) appended to {path}; translate them.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
