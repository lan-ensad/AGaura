# AGaura — project instructions

Voting portal for in-person general assemblies. Read [docs/SPEC.md](docs/SPEC.md) (what to build)
and [docs/PLAN.md](docs/PLAN.md) (in which order) before changing behaviour. Server installation
and operations: [docs/INSTALL.md](docs/INSTALL.md).

## Languages

- Code, identifiers, URL routes, comments, docstrings, documentation and commit messages: **English**.
- User interface text: **French**, always through gettext catalogs (`agaura/locales/`), never
  hard-coded in Python.
- Writing style in terminal replies, comments, docs, commit messages and diff or plan summaries:
  infinitive or imperative verbs ("Add …", "Reject …"; in French « Ajouter … », « Création du
  fichier »), impersonal or nominal phrasing, and no personal pronouns at all ("I", "me", "we",
  "us", "you"; « je », « me », « moi », « tu », « te », « toi », « nous », « vous »). User
  interface strings in the gettext catalogs keep their own wording.

## Commands

```sh
uv sync                                  # install
uv run uvicorn agaura.main:app --reload  # run locally
uv run pytest --cov                      # tests
uv run ruff check && uv run ruff format --check
uv run python scripts/demo_session.py    # full sessions through the services
uv run agaura audit verify               # operator CLI
uv run alembic revision --autogenerate -m "..."  # new migration after a model change
uv run python scripts/i18n.py check      # French translations complete (also a test)
uv run python scripts/dev_seed.py        # local account + session for manual testing
uv run python scripts/recount.py FILE    # recount a JSON export (docs/EXPORTS.md)
uv run python scripts/screenshots.py     # README screenshots (docs/screenshots/), after UI changes
docker compose up --build                # app + Caddy on https://localhost (mkdir -p backups/auto first)
```

## Layout

- `agaura/voting/` — pure tallying (no I/O, no database, no web). 100% test coverage required.
- `agaura/models/`, `agaura/services/` — persistence and business rules (phase P2). Services
  flush but never commit; callers wrap them in `Database.write()` (BEGIN IMMEDIATE) or
  `Database.read()`.
- `agaura/migrations/` — Alembic; `tests/test_db.py` fails if models and migrations diverge.
- `agaura/web/` — FastAPI routes and templates; conventions in [docs/WEB.md](docs/WEB.md)
  (shared files, i18n workflow, CSRF, real-time channels, message-code prefixes).
- `agaura/exports/` — CSV, JSON, PDF minutes and the independent recount; formats in
  [docs/EXPORTS.md](docs/EXPORTS.md). `agaura/pdf.py` renders PDFs offline (WeasyPrint).
- `tests/web/e2e/` — Playwright journeys in Chromium (phones in a mobile viewport).
- `deploy/` — Caddyfile, fail2ban filters and jails, server hardening guide (`deploy/README.md`);
  `deploy/update.sh` and `deploy/restore.sh` (docs/INSTALL.md §10); incidents in docs/RUNBOOK.md.
- `agaura/backups.py` — the only way to copy the database (`VACUUM INTO`, never while a poll of a
  real session is open) and the automatic backups; `agaura/services/retention.py` — D12 and
  purges of expired rows.

## Non-negotiable rules

- **Ballot secrecy**: never store a timestamp on `ballot`, `participation` or `voter_credential`;
  never link a ballot to a credential or a person; never log ballot content, session passwords or
  individual codes. The anonymity test in `tests/` must keep passing.
- **Atomic voting**: the participation record and the ballot are written in one transaction.
- **Account isolation**: every admin query filters on the current user (`owner_id`).
- **Ties are reported, never broken automatically.**
- Tallying uses exact arithmetic (`fractions.Fraction`), never floats, for thresholds and shares.
