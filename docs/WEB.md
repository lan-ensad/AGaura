# AGaura — Web layer conventions

How the web application is put together. Read with [SPEC.md](SPEC.md) (§5 requirements, §12.4
routes) before adding a page.

## Layout

| Path | Content | Owner area |
|---|---|---|
| `agaura/main.py` | App factory: middleware, static files, error pages, router order | shared |
| `agaura/web/deps.py` | Dependencies: `DatabaseDep`, `KeyringDep`, `HubDep`, `LimiterDep`, `SettingsDep`, `CurrentUser` (account holders; a super-admin gets the 403 page), `LoggedInUser` (any account), `SuperAdmin`, `OptionalUser`, `CsrfProtected`, `SameSiteOnly` (the console's downloads: refused when `Sec-Fetch-Site` is `cross-site`), `RowId` (an integer id in a path, bounded to SQLite's range: 422 past it, never a 500), `landing_page()`, `public_base_url()` | shared |
| `agaura/web/security.py` | Security headers + CSRF cookie middleware, `verify_csrf`, `refuse_cross_site`, cookie names (`*_COOKIE`, `cookie_name`) and `set_cookie` / `read_cookie` / `delete_cookie`, `client_ip`, `RateLimiter` | shared |
| `agaura/web/templating.py` | `render()`, `flash()`, Jinja filters (`long_date`, `short_datetime`, `time_of_day`, `percent`), `static_url()` | shared |
| `agaura/web/messages.py` | `ERRORS` (service error code → message), `NOTICES` (flash codes), `error_message()` | shared |
| `agaura/web/realtime.py` | In-memory hub, channels, `stream()` | shared |
| `agaura/web/public/` | Home, `/join`, `/login`, `/logout` | public |
| `agaura/web/account/` | Signup (open mode), invitations (`/invite/{token}`), verification, reset, account settings | accounts |
| `agaura/web/superadmin/` | `/superadmin`: accounts list, invitations, deletion (super-admin only) | accounts |
| `agaura/web/manage/` | Dashboard, session console (one module per topic, see below) | console (P5) |
| `agaura/web/live/` | Voter join page, live page, ballots, SSE | voters (P4) |
| `agaura/web/screen/` | Projection screen (secret link) | screen (P5) |
| `agaura/web/results/` | Public results page (secret link) | results (P6) |
| `agaura/pdf.py` | WeasyPrint rendering, offline (only `data:` URIs are fetched), one document at a time (`render_slot`; `PdfBusyError` → 503). A route that renders a PDF wraps its work in `manage.common.pdf_rendering(request, user)`, which also counts the account's PDFs (429 past 10 in 10 min) | shared |
| `agaura/exports/` | Ballots CSV, JSON export, PDF minutes, independent recount (`recount.py` depends on `agaura.voting` only) | results (P6) |
| `agaura/templates/macros/results.html` | Result block shared by the console, voter page and public page: `result_block(poll, data, show_counts=true, show_turnout=false)`, whose counts name blank ballots by the words of the ballot's blank choice ("Abstention", or "Vote blanc") (`blank_count_label`, also in the PDF minutes); `merit_bar(row, grade_count, valid, print=false, night=false)`; `duel_table(outcome, labels, matrix, print=false)`, the duels or strongest chains of a Condorcet result in the order of its ranking (`print`: no visually hidden text, for the PDF minutes); the rules (`condorcet_rule` and `condorcet_finding` among them), `names` and `ranked`, which the screen's night card and the minutes reuse | results (P6) |
| `agaura/templates/<area>/` | Templates of each area; `layouts/admin.html`, `layouts/voter.html`, `macros/forms.html` (`csrf`, `field`, `alerts`, `help(short, more)`: a help text folded to one line with "En savoir plus") are shared | |
| `agaura/templates/macros/icons.html` | `icon(name, label=none, size=none)`: a symbol of the sprite, decorative (`aria-hidden`) next to its text, or `role="img"` with `label` as its name when alone; `icon_button(name, label, tip=none, …)`: an icon-only button with `aria-label` and the same words as a tooltip (`.tip`). Import `with context`; `tests/web/test_icons.py` checks every name against the sprite | shared |
| `agaura/templates/macros/console.html` | `command_head(icon_name, title, rubric, anchor, link, id, note=none, control="", link_part=none)`: the head of a command of Steering (icon and title, optional muted note, then the way to its card: the icon of its rubric, `link`, a chevron; then `control`, the delegation of its right; `link_part` makes the words of `link` a live part); `ready_link(drafts)`: those words for `#ready`, with the count of the other drafts. Import `with context` | console (P5) |
| `agaura/templates/macros/delegates.html` | `delegate_badge(right)` (pill "Confié à …", a link to `#delegate-<id>`) and `hand_over(right, card)` (the pill and the "Confier" toggle whose floating panel creates a link from the card); see "Delegate links" | console (P5) |
| `agaura/static/icons.svg` | Icon sprite: Lucide 1.60.0 (ISC, licence in `static/LICENSE-icons.txt`), one `<symbol id="i-NAME">` each, plus AGaura's mark (`i-agaura`, `i-agaura-reversed`); used through `<use href>` (same origin, so allowed by the CSP), shapes only (no script, no style). A new icon is copied from Lucide unchanged and added here first | shared |
| `agaura/static/icons-voter.svg` | The few symbols of the voter pages, identical to those of `icons.svg` (checked by `tests/web/test_icons.py`), so the first load of a voter page stays light (NFR-04). The voter templates import `voter_icon as icon` from `macros/icons.html`: the sprite is chosen by the template, never by the context, so a padded vote step renders the same markup. A new voter icon is copied from `icons.svg` | voters (P4) |
| `agaura/static/css/app.css` | Shared styles (tokens, buttons, forms, cards, alerts, badges, tables, bars, grades, duel tables, icons) | shared |
| `agaura/static/css/<area>.css`, `agaura/static/js/<area>.js` | Per-area styles and scripts | area |
| `agaura/locales/fr/LC_MESSAGES/<area>.po` | French catalog of each area; all `.po` files are merged | area |

Routers are included in this order: public, account, superadmin, manage, screen, results, live. The live
router owns `/{session_id}` and its sub-paths, so it comes last; session IDs always contain a
digit and never collide with fixed routes.

## Console modules

The console is one router (`agaura/web/manage/common.py`) shared by several modules, so the
generated isolation tests see a flat list of routes:

| Module | Content |
|---|---|
| `common.py` | `router`, who is at the console (`console_access`, `ConsoleAccess`, `CurrentAccess`, `ConsoleUser`; session dependencies `OwnedSession`, `ConsoleSession`, `AgendaSession`, `PollsSession`, `TimerSession`, `HeadcountSession`, see "Delegate links"), `console_write()`, `owned_poll()`, `run_action()` (`in_place=True`: answered in place to a form of a live part, see "Live parts"), `from_script()`, `poll_action()`, `qr_svg()`, `voter_url()`, `ACTIONS` (session log labels), `@console_context` |
| `dashboard.py` | `/dashboard` ("Mes sessions"): the sessions grouped by state (in session, in preparation, over; most recent first in each), each row one link to its console; creation, the access mode as two choice cards (`.mode-card` of `manage.css`: radios hidden but focusable) |
| `session.py` | Console page and its checklist (`checklist()`: `Step(key, done, optional, missing, poll)`, what the details lack by name, the optional trial vote), live panel (`_panel.html`, see "Live parts"), console events, details, open, close, delete |
| `access.py` | Session password, access mode (`_access.html`), entry lock (status bar and the card `#entries`, `_entries.html`, which posts to `…/entries#entries`), headcount (`#presence`); provider `access_context` → `access_entries` (`AccessCap(cap, basis, members, headcount)`, the access cap of password mode and what it rests on: registered members, headcount or account quota; None in codes mode or for a delegate) |
| `codes.py` | Individual codes: generation, revocation |
| `polls.py` | The trial vote of the checklist (`POST …/polls/trial`, owner only, one waiting draft at a time, `Poll.trial`: see "Results and exports"), live control of polls, results on the projection screen (`POST …/polls/{poll_id}/screen`, see "Projection screen"); `…/publish` and `…/screen` redirect without an anchor, their forms post to `#poll-<id>` (Polls) or `#tab-cockpit` (the "Vote clos" command); provider `polls_context` (`polls_screen_detail_min`) |
| `editor.py` | Poll creation, the method first, and draft editing (see "Poll editor"), draft order, copy from another session; the template global `poll_kind(poll)` (the icon and name of a draft's method) |
| `links.py` | Projection screen link, public results link; provider `links_context` (`links_screen_url`, `links_results_url`, and `links_screen_view`, the `ScreenView` that `_screen_recap.html` puts in words; all None for a delegate) |
| `exports.py` | CSV, JSON, PDF minutes; provider `exports_context` (`exports_numbers`, poll id → number; `exports_polls`, the rows of Documents' "Par vote", owner only: `PollExports(number, poll)`, `ready` once voting has stopped) |
| `meeting.py` | Tribune and agenda: `POST …/tribune` (rows `name`/`role`; `more=1` is the "+" button without JavaScript: form sent back with one more row, nothing saved), the agenda card (`_agenda.html`: one text area, one item per line, then the choice of the item under discussion, none or any item), `GET …/agenda` (the card alone, `no-store`, for `agenda.js`), `POST …/agenda` (`items`, one per line; with `X-Requested-With: fetch` the answer is the card alone, saved, or refused with status 400 and its messages on the field), `POST …/agenda/current` (`item`: `next`, `previous`, `none` or a position; `seen` for the moves; answered in place to manage.js, 204 or 400, see "Live parts"); `meeting.js` adds tribune rows in place; `agenda.js` saves the agenda in place, marks unsaved changes (with "Annuler les modifications") and keeps them in `sessionStorage`, and follows the other consoles (see "Live parts"); attendance record of the headcount card (`_attendance.html`, included by `_presence.html` for the owner; provider `attendance_context` → `meeting_attendance`, rows from `agaura/exports/attendance.py`, data from `services/attendance.py`); message codes `meeting_…` |
| `notes.py` | Session notes: `GET …/notes` (the card and, in session, the "Quick note" command alone: `_notes_alone.html`, for `notes.js`), `POST …/notes` (`text`, `internal`; redirects without an anchor, the card posts to `#notes`, the command to `#cmd-notes`), `POST …/notes/edit` (`note`, `text`, `internal`, `item`), `POST …/notes/delete` (`note`); with `X-Requested-With: fetch` the answer is both copies, or the error message with status 400; message codes `notes_…` |
| `timer.py` | Chronometer of the projection screen (FR-74): `POST …/timer/{start,pause,resume,reset,add,show}`, redirecting without an anchor (the card posts to `#timer`, the command to `#cmd-timer`); `GET …/timer` (the card and, in session, the command alone: `_timer_alone.html`, for `timer.js`); message codes `timer_…` |
| `delegates.py` | Delegate links (FR-92): owner card `_delegates.html` in Settings, where links are listed, changed and revoked, one tile per link with nothing folded (address, copy, QR code, the form of its name and rights) and a "+" in the head, a link to the tile of a new link (`#delegates-new`, folded by `manage.css` until it is the target, so without script too; shown from the start while there is no link) (`POST …/delegates` with `name` and `rights`, answered at `#delegate-<id>-link`, or at `#delegates-new` after an error; `…/delegates/edit` with `delegate`, `…/delegates/revoke`); in the head of each card of a right (timer, headcount, agenda, session notes, drafts) and of its command in Steering, `hand_over(right, card)` of `macros/delegates.html`: the pill `delegate_badge(right)` ("Confié à {name}": the first link that holds the right, then how many others, every name in its `title`; a link to `#delegate-<id>`) and one "Confier" toggle that creates a link in place (`POST …/delegates` with `card`, one of `HAND_OVER_CARDS`: back to `?hand=<card>&link=<id>#<card>`, the card open on the new link; with `X-Requested-With: fetch`, `_hand_over.html`, every slot and the Settings card for `manage.js` to swap); provider `delegates_context` (`delegates_links`, `delegates_holders` (right → `Holder(id, name)`) for the pills, `delegates_hand_over` (owner, session not ended, fewer links than the maximum), `delegates_open` and `delegates_created` (the `hand` and `link` query parameters, validated; a GET writes nothing), `delegates_names` for the session log; all empty for a delegate); `entry_router` (no account, outside the console router): `GET`/`POST /manage/{id}/delegate`, `POST /manage/{id}/delegate/leave`; message codes `delegates_…` |

The console page (`manage/console.html`) is a sidebar of rubrics (`_nav.html`; tabs at the
bottom of a phone: the rubric the console lands on, Votes, Ordre du jour, Accès, then "More"
holding the others, in the sidebar's order; a delegate link gets a tab per rubric and no "More";
one icon per rubric, the same in the three places), a status bar (`_status.html`) and one `<section class="rubric" id="tab-…">` per
rubric, in this order. Steering (`tab-cockpit`, "Conduite") exists once the session is open: in
preparation it is absent from the sidebar, the tabs (four, sharing the bar) and the page, and
the console lands on Séance (`tab-meeting`; `landing` and `data-default` in `console.html`; a
delegate link, which never sees Séance, lands on the first rubric of its rights, headed by
`_delegate_home.html`). The status bar shows "Bulletins" (the ballots cast) while a single poll
is open, the count of polls open otherwise; on a phone (below 960 px) its "Terminer la session"
gives way to the `.session-end` card at the foot of Steering, the same form and `data-confirm`
(two forms on the page, `manage.css` shows one or the other):

| Rubric (`id`) | Label | Partials |
|---|---|---|
| `tab-cockpit` | Conduite | none in preparation; in session: `_cockpit.html`, commands only: `#just-closed` ("Vote clos", `state.just_closed`), `#live` with `#live-panel` (`_live_polls.html`), `#agenda-now` (its body `_agenda_now.html`), `#ready` (the next poll, its body `_ready.html`), then the compact copies `#cmd-notes` (`_notes.html`), `#cmd-timer` (`_timer.html`), `#cmd-presence` (`_presence.html`), `#cmd-join` (`_join.html`); at the foot `.session-end`, the owner's "Terminer la session" on a phone; closed: `_summary.html` (the minutes as the primary action and the complete data, then three commands that lead to their card: `#after-results` (Votes, `#results`), `#after-public` (Documents, `#public-results`), `#after-fix` (Séance, `#tribune`, and the notes, `#notes`)). A delegate: `_delegate_home.html`, then its commands |
| `tab-polls` | Votes | every poll a tile folded by default (`tile_head` of `macros/polls.html`: a details element whose summary holds the title and the pills of its state only, the rest, actions included, once unfolded; `manage.js` unfolds the tile an address targets and keeps the unfolded ones open when a live part is redrawn): in session, `_polls_open.html` (`#polls-open`, the polls open now, `_live_poll_tiles.html`, tiles `#open-poll-<id>`, shown only while one is open); `_drafts.html` (its head leads to the creation page, "Nouveau vote"; tiles `#draft-<id>`, each with the icon of its method once unfolded), `_finished.html` (closed: results first; tiles `#poll-<id>`) |
| `tab-meeting` | Séance | the owner's only: in preparation, `_checklist.html` first (`#checklist`: one short line per step of `session.checklist()`, what it lacks or holds, the icon and short name of its card's rubric when another; the trial vote's form, back to `#checklist`; no count in the sidebar); `_details.html` (read only once the session is open) beside a column with `_timer.html` (`#timer`, the chronometer, the owner's copy) above `_tribune.html` |
| `tab-agenda` | Ordre du jour | `_agenda.html` (the sidebar counts the items, or "2/5" with an item under discussion); absent once a session without an agenda is closed |
| `tab-notes` | Notes de séance | `_notes.html` |
| `tab-access` | Accès et présences | two pairs of cards (`.rubric-pair`): `_join.html` and `_entries.html` (`#entries`: the state of entries, the lock, the access cap explained), `_presence.html` (with the attendance record, `#attendance`) and `_access.html` (access mode, password); then `_codes.html`. A headcount delegate: `_presence.html` alone |
| `tab-screen` | Écran de projection | `_links.html` (screen link) and `_screen_recap.html` (`#screen-now`, "Ce que la salle voit") side by side; a delegate holding `screen` gets `_screen_recap.html` alone, one holding `timer` the chronometer (`_timer.html`, which the owner has in Séance) |
| `tab-documents` | Documents | `_exports.html` (`#exports`: minutes, complete data, a pointer to the code cards in codes mode; `#poll-exports`: "Par vote", one row `#exports-<poll id>` per poll pushed), `_public_results.html` |
| `tab-settings` | Réglages | `_delegates.html`, session log (with the delegate's name next to its actions), deletion |

Buttons have four levels (`app.css`): `.button.primary` once per card at most, for the next step of the session; `.button` alone (secondary) for the other actions; `.button.quiet` for a way out or a cross-link ("Annuler" in the editor, "Aller dans Conduite"); `.button.danger` for the last step of an irreversible action only. In the commands of Steering the primary buttons are "Publier le résultat aux votants" of the poll just closed, "Clore le vote" when one poll is open (none when several are), "Suivant", and "Lancer le vote" of the next poll; in Polls, the first draft and the most recent result voters do not see. The tools (notes, chronometer, headcount) keep secondary buttons during the session, in their command and in their card, except for a delegate link that holds one of them and no step of the session (`can.lone_tool`); outside the session their card leads. A card rendered alone for a script (`GET …/timer`) gets `can`, so its buttons keep the level they have on the page.

Each draft shows one action (push in session, edit before) and a "⋯" menu (`details[data-menu]`, closed by `manage.js` on a click elsewhere or Escape) with the rest: push with a timer, edit, duplicate, delete. One feature, one card, one rubric (D-A): each card exists once on the page,
in its rubric in every state of the session, and keeps its id (`#notes`, `#timer`,
`#presence`, `#join`). During the session Steering adds commands: compact copies rendered by
the same partial with `compact`, whose ids take the prefix `cmd-` (`#cmd-notes`, `#cmd-timer`,
`#cmd-presence`, `#cmd-join`; the commands that are Steering's own keep theirs: `#live`,
`#agenda-now`, `#ready`, `#just-closed`). A command acts without any setting and leads to its
card (`command_head` of `macros/console.html`); its forms post to `…#tab-cockpit` or to its
own id, so the routes that a card and a command both use redirect without an anchor. The
partial's script tag comes with the card only, and the script handles each copy by itself
(`[data-timer-url]`, `[data-notes-url]`), never by id. `manage.js` shows one rubric at a time, the one the address
names (`#tab-…`, or the id of anything inside a rubric: `#notes`, `#draft-3`, which also opens
the `<details>` around it), else the one marked `data-default` (Steering once the session is open, Séance before); `console-boot.js` (in `<head>`) hides the other
rubrics before the first paint. Without script every rubric stays visible, one after the other.
To come back to the rubric a form was sent from, `manage.js` adds that rubric to the form's
address when it has no fragment; the server redirects without one, and the browser keeps the
fragment of the address it posted to. So an action that two places trigger
(`agenda/current`, `polls/{id}/open`, `polls/{id}/publish`, `polls/{id}/screen`, `entries`,
`headcount`, `notes`, `timer/…`) redirects without an anchor, and its forms post to `…#agenda`,
`…#tab-cockpit`, the id of their card or command, or nothing.

A module that needs more variables on that page registers a provider with `@console_context`;
its keys carry the module's prefix. A provider receives the `ConsoleAccess` and runs for
delegates too: it returns the same keys, emptied of what the delegate's rights do not cover
(`links_context` never decrypts the secret links for a delegate, `notes_context` gives notes
only with the agenda right). A console form that must keep its values on error
re-renders the console page (`session.session_console`) with status 400, the typed form passed
through `request.state` to its provider, and posts to an address ending in `#<card id>`.

Live parts: `GET /manage/{id}/panel` answers `_panel.html` (`no-store`; not through `render()`,
so the flash notice stays for the console page), one `[data-live-part]` block per part that
another console may change, each only for whoever may see it: the status bar (`status`), the polls
open now (`polls`, polls right) and, during the session, the bodies of two commands of Steering
under their heads, the item under discussion (`agenda-now`, `_agenda_now.html`, agenda right) and
the next poll (`ready`, `_ready.html`, polls right, its drafts from `console.drafts()`, one
owner-filtered query) with the count of drafts in its head (`ready-link`, `ready_link`), and the
list of "Ce que la salle voit" (`screen-now`, `_screen_recap_list.html`, owner or right `screen`,
from `links.recap_context`); the heads, "Confier" included, stay out of the parts. The answer holds nothing else, since it is fetched on
every event. On each `turnout` event `manage.js` swaps each one into every element of the same
name on the page (the polls open now: `polls`, `_live_polls.html`, without ids, in the "Votes en
cours" command's `#live-panel`, at one click; `poll-tiles`, `_live_poll_tiles.html`, the folded
tiles of Polls' `#polls-open`, a details element unfolded there staying so in the new copy), only
when its HTML changed (white space
around it aside: the page indents its copies, so a script that sets an attribute inside a part
counts as a change); when the focus was inside, it goes back, without scrolling, to the element
with the same id, else to the button with the same `name` and `value` in a form with the same
address (to the same address for a link), else, that button being disabled now ("Suivant" at the
last item), to another button of that form, never to one of another form. It then shows or hides
each `[data-shown-with]` card by whether its selector matches inside it, reveals the
`[data-refresh-hint]` notices on a `polls` event (a timer closed a poll), reloads the page when
the state of the session changed (`data-status`) or the rights of the delegate link it was opened
with (`data-access`), and fires `agaura:console-refreshed` on `document`, on which the cards that
follow other consoles (`timer.js`, `notes.js`, `agenda.js`) refetch their own fragment, both
copies in one answer, since every console action publishes `turnout`/`console`: `run_action` (agenda, item
under discussion, drafts moved, duplicated, deleted, pushed, copied from another session),
`editor._saved` (drafts created, edited or launched), the notes and the hand-over. `timer.js`
also counts on the status bar's clock. A form marked `data-live-form` ("Précédent"
and "Suivant" of `#agenda-now`, the choice of the item in the agenda card) is sent by `manage.js` with `fetch` (`X-Requested-With: fetch`,
the button pressed added), one at a time; its route, `run_action(…, in_place=True)`, answers 204
without a notice, and `manage.js` fetches the panel at once, so the page neither reloads nor
scrolls and the button keeps the focus; a refused action answers 400 with its message in the
flash cookie, and `manage.js` loads the page again to show it. Without script, or in a browser
that does not name the button pressed, the form posts and redirects as any other. The agenda
card (`agenda.js`) never replaces a text being edited: its text area takes the new agenda only
while it holds no change of its own and has not the focus (else once the focus leaves it), and
kept changes show a notice that the agenda changed elsewhere meanwhile; its list of items is
updated in place (the focus stays) unless it holds a choice not sent yet. The other cards of the
page, the drafts of Votes among them, follow only after a reload.

Console paths take only `{public_id}`, `{poll_id}` and `{batch_id}` as parameters (the isolation
test fills them); every other identifier goes in a form field. Every console route takes exactly
one session dependency (`OwnedSession` unless a delegate right covers it) and `ConsoleUser`;
`tests/web/manage/test_delegates.py` pins the routes open to each right in `DELEGABLE`. Every GET route answers 200 to the
owner, except the reads listed in `NOT_READY` of `tests/web/manage/test_isolation.py`.

## Rules

- **Language.** Code and source strings in English; every visible string goes through `_()`
  (templates and Python) or `N_()` (module-level constants, translated when displayed). Never
  write French in Python or templates. Add translations with
  `uv run python scripts/i18n.py add <area>`, fill each `msgstr`, then
  `uv run python scripts/i18n.py check` must report nothing for your strings. JavaScript holds no
  visible text: it reads strings from `data-` attributes rendered by the templates. French
  typography: a no-break space (U+00A0) before « : », « ; », « ? », « ! » and inside « », in the
  catalogs and in the few signs a template writes itself (`</strong> :` of "Ce que la salle
  voit", the address of the screen); tests that read raw HTML write it `\u00a0`, those that
  read the text of a page (`text_of`, which splits on white space) a plain space.
- **Templates.** Extend `layouts/admin.html` (account holders) or `layouts/voter.html` (voters).
  Import macros `with context`: `{% from "macros/forms.html" import csrf, field with context %}`.
  `render(request, name, context, status_code=...)` provides `csrf_token`, `user`, `notice` and
  `error` (set `error` in the context to show an error box, `#page-error`). A field refused by
  that error takes `field(..., error_id="page-error")`: `aria-invalid`, described by the
  message, and `autofocus`. A page's notice (`#main > .alert`) goes as soon as a console form is
  sent (`manage.js`), so an action sent in place never leaves the previous one's notice.
- **Rate limits.** Counters of `app.state.limiter` take `limits.limit_address(request)` (IPv6
  grouped by /64), never the raw address. Voter-side limits live in `agaura/web/limits.py`: joins
  (per address, per session, per address on a session under attack), the tribune alert, unknown
  session IDs. Before a step that depends on a secret (a password, a code), count the attempt
  with `limiter.charge({key: (limit, seconds), …})` (check and count in one step, so concurrent
  requests cannot all pass) and `limiter.refund(*keys)` when it was not a failure;
  `limits.charge_join_attempt` / `refund_join_attempt` / `join_alert_due` do it for joins.
- **Passwords.** `hash_password` and `verify_password` run two at a time in the process
  (`agaura/security.py`; `PasswordBusyError` → 503). A route that hashes inside a write
  transaction takes its turn first: `with password_slot(), database.write() as db:`.
- **Phone widths.** Phones are 360 to 430 CSS pixels wide, 320 for the smallest: no page scrolls
  sideways from 320 px (`tests/web/e2e/test_console_layout.py` checks the dashboard, the account
  and every console rubric). Grid columns use `minmax(0, …)`, so a wide table scrolls inside its
  `.table-wrap` instead of widening the page; on the dashboard each session is one block whose
  state and figures go under the title below 560 px.
- **CSP.** `default-src 'self'`: no inline `<script>`, no `style=""` attribute, no external
  resource. Widths of bars use `<progress>`; QR codes are inline SVG generated server-side
  (`segno`).
- **Forms.** Every state-changing route has `dependencies=[CsrfProtected]` and every form
  includes `{{ csrf() }}`. JavaScript `fetch` calls send the token in the `X-CSRF-Token` header
  (read it from the form's hidden `csrf_token` input). Use the POST/redirect/GET pattern
  (`RedirectResponse(url, status_code=303)`), with `flash(response, request, code)` for a notice.
- **Database.** Wrap service calls in `with database.write() as db:` (one transaction, committed
  at the end) or `with database.read() as db:`. Publish real-time events and set cookies *after*
  the `with` block, once the transaction has committed. Never keep a write transaction open
  while doing slow work (email, rendering a PDF).
- **Errors.** Services raise `ServiceError` subclasses with a `code`; show
  `error_message(error)`. `NotFoundError` raised from a route becomes the 404 page. Areas add
  their own codes with `ERRORS.update({...})` / `NOTICES.update({...})` at import time of their
  module, using `N_()`. Both dictionaries are one global namespace: a new area-specific code
  takes the area as prefix (`account_password_changed`, `live_…`) so it never overwrites another
  area's message.
- **Cookies.** Use `set_cookie()` / `read_cookie()` / `delete_cookie()` from
  `agaura.web.security`, never `request.cookies` or `response.set_cookie` directly: `HttpOnly`,
  `SameSite=Lax`, `Secure` in production only, so a phone on the local network can test over
  plain HTTP. Every name is a constant of `agaura.web.security` (`SESSION_COOKIE`, `CSRF_COOKIE`,
  `FLASH_COOKIE`, `VOTER_COOKIE`, `DELEGATE_COOKIE`), given as is in development; in production
  `cookie_name()` prefixes it with `__Host-` (cookies on `Path=/`) or `__Secure-` (cookies of
  `PATH_SCOPED_COOKIES`, on a session's path), and only the prefixed name is read. A new cookie
  scoped to a path goes into `PATH_SCOPED_COOKIES`; `set_cookie` refuses a name on the wrong
  path. JavaScript never reads a cookie (the CSRF token comes from the form).
- **Addresses shown to voters** (QR codes, links): `public_base_url(request)`, which follows the
  address used to reach the console in development.
- **Secrecy.** Never log or put in the audit log a session password, a voter code, a token or
  ballot content. Never add a timestamp or an identifier to anything a voter submits.

## Delegate links

FR-92. `agaura/services/delegates.py` (model `agaura/models/delegate.py`), `agaura/web/manage/
delegates.py`, `templates/manage/{_delegates,_delegate_home,delegate_join,delegate_ended}.html`,
`static/js/delegate.js`.

- **Who is at the console.** `common.console_access` (cached in `request.state.console_access`)
  resolves, in order: a super-admin (403 page, before any session is read); the owner, logged in;
  a delegate link of this session, from the `DELEGATE_COOKIE` cookie (`agaura_delegate`,
  `__Secure-agaura_delegate` in production; path `/manage/{ID}`); else
  the page of an ended link (`DelegateEndedError`, 403) when such a cookie is there, the login
  page for an anonymous visitor, 404 for another account. `ConsoleAccess.user` is always the
  owner of the session: services keep their owner filter.
- **Rights.** `Right`: `agenda` (agenda, item under discussion), `notes` (session notes),
  `polls`, `timer`, `screen` (what the room sees: the card `#screen-now` and its buttons),
  `headcount`. The session dependency of each
  route decides: `OwnedSession` (owner only: a delegate gets 404), `ConsoleSession` (any
  delegate: console page, panel, events), or one dependency per right. Templates read
  `can.owner`, `can.agenda`, `can.notes`, `can.polls`, `can.timer`, `can.screen`, `can.headcount`; a delegate sees
  Steering once the session is open (`_delegate_home.html`, its rights leading to their cards,
  then its commands; in preparation that card heads the rubric it lands on, Séance or the first
  of its rights) and the rubrics of its rights with their cards only, in every state: Votes,
  Séance (agenda, notes; never the details or the tribune), Accès et présences (the headcount
  only), Écran de projection (the chronometer, "Ce que la salle voit", never the screen link); each as a tab of
  its own on a phone (no "More"). Also the status bar without the entry lock and session
  actions, and the top bar "Accès délégué : {name}" with "Quitter".
- **Handing over** (D-G, as the owner changed it on 2026-11-04). The card of a right, and its
  command in Steering during the session, shows `hand_over(right, card)` in its head: the pill
  of its holder (a link to the link in Settings) and one compact "Confier" toggle
  (`details.hand-over[data-menu]`, `name="hand-over"`: one open at a time; closed by default,
  its accessible name "Confier le chronomètre à une personne", its summary exposed as expanded
  or collapsed). Open, the toggle keeps its place and size and looks pressed (filled); its
  panel floats over the card (`manage.css`: absolutely positioned, the card its containing
  block, under the toggle's line, on the card's right edge, 24rem on a computer, the card's
  width below 600 px), so the card neither grows nor moves, and `manage.js` scrolls just enough
  to show it above the bottom tabs of a phone. It holds a new link form (a name, the card's
  right ticked, the other rights folded, "Gérer les liens, dans Réglages"); ids take the card id
  as prefix (`cmd-timer-hand-name`). A tap elsewhere leaves it open: its toggle, Escape (the
  focus back on the toggle) or its link to Settings close it. Without script the form posts
  with `card` and comes back to that card open on the new link (address, copy, QR code); with
  script `manage.js` sends it with `fetch` and swaps every `[data-hand-over]` slot and
  `#delegates` from the answer, the new link in the same floating panel, then "Fermer"
  (`data-hand-over-close`, script only), the toggle or Escape dismiss it and give back the form,
  rendered hidden under the link (none once the links are at their maximum). Changing and
  revoking links stay in Settings. `tests/web/e2e/test_delegates.py` checks every card and
  command at 1280 by 800 and 390 by 844 (`test_hand_over_is_a_stable_toggle`).
- **Link.** `/manage/{ID}/delegate#k={token}` (`security.new_token`, 256 bits): `delegate.js`
  moves the key from the fragment to the form (`history.replaceState`) and submits;
  `POST /manage/{ID}/delegate` checks it (`delegates.authenticate`: link in use, session in
  preparation or in session, owner account enabled), sets the `HttpOnly` cookie for 30 days and
  redirects to the console. Wrong keys: 30 per address in 10 minutes, then 429. Entry pages:
  `no-store`, `no-referrer`, `noindex`.
- **Audit.** Write through `console_write(request)` (used by `run_action`, the notes script
  path and the editor): when the request comes through a link, `audit.act_as_delegate` makes
  every `user` entry of the transaction a `delegate` entry with `details.delegate`. Link
  entries (`delegate.created`, `.updated`, `.revoked`) carry the id and the rights, never the
  name or the key.
- **End.** Revoking (or closing the session: `assemblies.close_assembly` calls
  `delegates.end_all`) sets `ended_at` and clears `token_hash` and `token_enc`; the console
  event makes an open delegate console refetch its panel, get 403 and reload into the page of an
  ended link, which offers to remove the cookie.

## Accounts and login

- Login cookie `SESSION_COOKIE` (`agaura_session`, `__Host-agaura_session` in production), for
  7 days; the server ends the session after 12 h without activity and 7 days after the login
  (`auth.SESSION_LIFETIME`, `auth.SESSION_MAX_AGE`). A login, a signup or an accepted
  invitation first ends the session of the cookie the browser sent. `CurrentUser` admits
  account holders only: anonymous visitors are redirected to `/login?next=…`, a super-admin
  gets a 403 page (raised before any session is looked up, so the console never reaches session
  data). `LoggedInUser` admits any account (`/account`), `SuperAdmin` the super-admin only (others get
  404), `OptionalUser` everyone.
- After login: `/superadmin` for a super-admin (`next` kept only under `/superadmin` or
  `/account`), `/dashboard` otherwise (`deps.landing_page()`).
- Signup is closed unless `AGAURA_SIGNUP=open`; templates read
  `request.app.state.settings.signup_open`. Accounts come from invitations: `accounts.invite`
  creates an account with `password_hash = None` and an `invite` token (7 days);
  `/invite/{token}` sets the password, confirms the email and logs in. Pending accounts can
  neither log in nor receive a reset link.
- The super-admin is created with `agaura user create EMAIL --superadmin` only; it cannot create
  sessions (`assemblies.create_assembly` refuses) and the web never deletes a super-admin. Its
  actions are audited with actor `superadmin` and the target account id only. Deleting an
  account asks for its address and the super-admin's own password, counted like the forms of
  `/account` (`account.common.password_limits` / `password_attempt`).
- Creating a session requires a verified email (FR-02): `user.email_verified_at is not None`.
- Services: `agaura/services/auth.py` (login sessions), `agaura/services/accounts.py` (accounts,
  quotas, invitations, super-admin and operator actions).

## Voters

- Device cookie `VOTER_COOKIE` (`agaura_voter`, `__Secure-agaura_voter` in production), scoped
  to the session path (`path=/{PUBLIC_ID}`), holding the token returned by `access.join`;
  `access.credential_for(db, public_id, token)` checks it. Once the session is closed it votes no
  more, but `access.former_credential` still recognizes the device: `live/status.html` then shows
  the results published to voters (`live.results_for_voters`, `results_title`, `result_section`)
  under the closing message, and the cookie is kept; any other visitor gets the message alone and
  loses a stale cookie. The page of a plain-form ballot step (`live/poll.html`) shows the demo band
  through `demo_session`, as the live page does through `assembly`.
- Canonical session URL: `/{PUBLIC_ID}` in upper case (`assemblies.normalize_public_id`); other
  spellings redirect to it.
- The live page shows only open polls and results published to voters, each group most recent
  first (FR-41). The session title of the top bar is the page's `h1` (`title_heading` set by
  the `live/` templates), poll and group titles `h2`. From two open polls on, a summary
  (`open_summary`, `data-summary`) links to the polls still to vote on the same page; a poll
  voted on shows on one line (state `recorded`), the full confirmation (state `voted`) only as
  the answer of the ballot step, after which live.js refreshes the summary alone.
- live.js merges a refresh section by section (`data-key`): it removes the sections that go
  before placing the others, so a section kept is never moved; it keeps a section that holds
  the focus; it scrolls by what changed above the section being read (`keepReading`; the
  browser's own scroll anchoring is off); it reads out each poll that appears in the
  `[data-announce]` region. The blank vote is the last choice of every ballot, right after the
  last option, at its level and drawn like it, always named `blank_label(poll)`, the summary
  included: a `.choice` row (`blank_choice`: a radio button of the group in single choice, a
  box in approval), a `.grade-pill` after the last option's grades (`blank_grade`, `.mj-blank`),
  a `.rank-option` row with a box in the column of the lists (`blank_rank`, `.rank-blank`).
  Its box (`data-blank`) is exclusive: live.js clears the options, grades and ranks when it is
  checked and unchecks it when one of those is chosen; without JavaScript the server refuses it
  with other choices (`live_blank_with_choice`), giving the ballot back with those choices and
  the box unchecked.
- Ballot box (`live/macros.html`, SPEC §6.6, FR-43): the counters sit right after the last
  choice, the blank vote (`role="status"`, hidden without JavaScript). The approval counter
  (`approval_counter`, `#poll-<id>-counter`, which describes every box) carries its texts in
  `data-` attributes (`data-left-one`/`-other`, `data-full`, `data-help`, `data-over-…`,
  `data-count-…` without a maximum; `-one`/`-other` follow the plural rule of the catalogs,
  `{n}` the count); at the maximum live.js sets `aria-disabled="true"` (never `disabled`) on
  the option boxes left unchecked (never the blank one) and their row `.is-blocked`, refuses
  their click and reads the counter out again; the server still refuses an over-vote
  (`too_many_choices`). The review
  and the confirmation open with the steps of the ballot box (`steps`, `.stepper` of
  `app.css`, `aria-current="step"`); the summary with `.pending-flag` ("Bulletin pas encore
  déposé"), and an ungraded option shows the worst grade as `.grade.is-default` (dashed). A
  Condorcet ballot is one `<select name="rank-<position>">` per option (`condorcet_ballot`,
  "Non classée" first with the value `00`, then the ranks; `.rank-option`), its counter of
  ranked options (`ranked_counter`, `data-ranked`) empty in every answer and written by
  live.js, which also empties the lists when the blank box is checked and unchecks the box when
  a rank is chosen; its summary lists the options best first (`Choice.by_rank`,
  `.summary-ranks`), "Non classée" as `.rank.is-default` (dashed). The
  confirmation (state `voted`) has no status sentence (its focused heading says it), except
  "Vote déjà enregistré" (409), and an empty, hidden `[data-next-vote]` link. live.js writes
  what depends on this device or on the other polls, never sent by the server: the "À
  confirmer · …" text and `.is-review` of a summary link whose poll is in review
  (`data-msg-review` of `open_summary`), and the "Vote suivant : …" target, the first pending
  poll of the summary below the confirmation, else the first above (`.is-above` turns its
  arrow). While a ballot step is sent, its section is `aria-busy` and its buttons
  `aria-disabled`: a second tap does nothing and the focus stays.
- Lost connection (FR-42): `live/page.html` holds `[data-net-banner]`, a `role="status"`
  always in place and empty (`.net-banner` of `app.css`: flat until filled), with its texts in
  `data-msg-lost`, `data-msg-help`, `data-msg-back`. live.js calls `trouble()` when the stream
  errs, a request fails (network error, 5xx, a refresh with no answer within 10 s) or the
  browser goes offline: a refresh asks the server at once, and without any answer (`reached()`:
  stream opened or event, refresh or ballot step answered) for 5 s the banner shows, fixed at
  the bottom, and polling starts. The first answer turns it to "Connexion rétablie" for 3 s.
  The page gains room below its last line the first time (`html.net-room`, kept, so the page
  never shrinks under the reader) and `scroll-padding-bottom` while it shows (`.net-trouble`).
- Join page: one line under the form states the secret (`.join-promise`, FR-40a), the same
  text for every session and in the neutral form; change it with `macros/ethics.html`.
- Join form in password mode: `inputmode="numeric"` when the current password is made of
  digits only, and the hint says "8 digits" for the generated shape
  (`routes._password_field`).
- Scanning addresses (`limits.scanning`: 40 unknown IDs in 10 minutes, the counter going on):
  a device without a credential for the ID gets, on every well-formed ID, the neutral form of
  `live/join.html` (`assembly` is `None`: no title, no state, one field for passwords and codes,
  the same `data-autojoin` hook for `#k=`), status 200; other spellings redirect before any
  lookup. `POST /{ID}/join` still joins with the right secret under the usual join limits, and
  answers every other outcome (unknown ID, session not in session, wrong secret, locked entries,
  access cap reached, session lockdown) with that form, status 400 and the error
  `live_join_failed`; an address over its own join limit gets 429 and `too_many_attempts` for
  every ID. Nothing in these answers may depend on the session: they would tell an existing ID
  from a missing one.
- Access cap (FR-27): in password mode `access.join` refuses a new device once
  `access.access_cap(assembly)` credentials exist, counted inside the join's write transaction;
  the route answers 409 with `access_cap_reached` and never records it as a join failure (the
  password was right). `LivePanel.access_cap` is set while the cap is reached, and the status
  bar shows `#access-cap` to the owner and the delegates; the join that reaches the cap already
  publishes `turnout`, so the console shows it at once.
- Ballot steps (`POST /{ID}/polls/{poll_id}/review` and `POST /{ID}/polls/{poll_id}`) have one
  size per poll (SPEC §9.2): `routes._section` renders the poll's section through
  `padding.padded_section`, which ends it with an HTML comment up to the largest section any
  choice and step can give (`padding._extremes`; `tests/web/live/test_padding.py` walks every
  choice of small polls). A new part of `live/macros.html` that depends on the choice, the step
  or the message must be covered there (the dashed default grade, the `selected` ranks and the
  hint after an unranked option are parts of the choice; the
  steps, the flag and the empty "Vote suivant" depend on the step only). What depends on the
  other polls or on this device is written by live.js, never in an answer. Every position and
  rank a ballot form sends takes two digits (`ballot_number`, `ballot_grade_name`,
  `ballot_rank_name`; the blank radio button of single choice and "Non classée" are `00`,
  `ballot_forms.BLANK_CHOICE` and `UNRANKED`), so that a browser without JavaScript sends one
  size too (SPEC §9.2). Their forms carry `data-pad` (`ballot_request_size`):
  live.js adds a `pad` field up to that size, and the server never reads it. Caddy does not
  compress `/{ID}/polls/…` and the application compresses nothing.

## Real time

`HubDep` gives the hub. Events carry no data that matters; pages refetch their state.

| Channel | Event (data) | Published by | Listened by |
|---|---|---|---|
| `live_channel(public_id)` | `update` (`polls`, `results`, `session`) | console, after any change voters can see; poll timers | voter live page |
| `manage_channel(public_id)` | `turnout` (`""` join or ballot, `console` console action, `polls` timer) | voter routes, `run_action`, session deletion, poll timers | session console, projection screen |

SSE endpoints use FastAPI's native support. Each stream holds a socket of the single process:
an `async` dependency with `yield` holds the stream's slot (`hub.hold(channel, Holder(kind,
key))`, given back once the answer ends) and answers 429 past the caps of `realtime.py`
(`STREAM_CAPS` per holder: voter credential 4, screen link 6, owner or delegate link 8 per
session; `MAX_STREAMS_PER_CHANNEL` 2,000; `MAX_STREAMS` 3,000). `stream()` takes a blocking
`allowed()` that checks the holder's access again, in a worker thread, before each event and at
least every `RECHECK_INTERVAL` (30 s); the stream ends at the first refusal:

```python
from fastapi.sse import EventSourceResponse, ServerSentEvent

async def _events_slot(hub: HubDep, response: Response, follower: ...) -> AsyncIterator[...]:
    with hub.hold(live_channel(follower.public_id), Holder("voter", (credential_id,))) as held:
        if not held:
            response.status_code = 429
        yield follower if held else None

@router.get("/{session_id}/events", response_class=EventSourceResponse)
async def events(...) -> AsyncIterator[ServerSentEvent]:
    async for message in stream(hub, live_channel(public_id), allowed):
        yield ServerSentEvent(event=message.event, data=message.data)
```

FastAPI writes every `data` as JSON: `polls` reaches the page as `"polls"`, with its quotes;
`manage.js` decodes it (`payload`) before comparing, which shows the hint to reload the console
when a poll closed by its timer. Clients reconnect automatically (`EventSource`) and fall back
to polling every 5 s; a stream
refused (429, 204, 404) or ended makes the page poll, then try again. In tests, a stream answer
returns only once it ends: `tests/web/streams.py` acts from another thread meanwhile; a
`TestClient` opened without `with` runs on its own event loop and cannot share the hub's
streams.

## Poll editor

One flow, the method first (D-C), on its own page for every way of creating a poll ("Nouveau
vote" in the head of the drafts, the "Prochain vote" command of Steering), in two steps rendered
by the server (`agaura/web/manage/editor.py`, `templates/manage/{editor,_editor_methods,_editor_form}.html`,
`static/js/editor.js`, `static/css/editor.css`):

- step 1, `GET /manage/{id}/polls/new`: the voting methods as four cards (the modes of
  `editor.KINDS` without a `card`: `single_choice`, `approval`, `majority_judgment`,
  `condorcet`), each a link to step 2 (`editor.opening()`: "Choix unique" opens on its
  resolution form, `?kind=resolution`) whose
  title is stretched over the card. `editor.GROUPS` lists groups of cards, the voting methods
  for now, so that a group apart may join later (an animation with no voting value). The
  owner's copy of a past session's polls sits under the cards (`#copy`, `POST …/polls/copy`
  with `source`, the public ID of another session of the same account; never for a delegate);
- step 2, `GET …/polls/new?kind=…` (an unknown `kind` gives step 1) and
  `GET …/polls/{poll_id}/edit` (a draft, its method from `kind_of(poll)`: a single choice with
  `adopt_option` is a resolution): the method (its card, `card_of()`) recalled with "Changer
  de mode", then the forms of that card when it has several (`editor.FORMS`: "Résolution" and
  "Options libres" for "Choix unique", submits `switch=<kind>` with `aria-pressed`), then only
  that method's fields. A resolution stays a mode of its own (`KINDS["resolution"]`, with
  `card="single_choice"`): stored polls, the trial vote and the demo keep it, and the drafts
  list and Steering name it "Résolution". Each method starts from `fields_from_kind()`, never
  from the last poll; free options start with two empty rows (`MIN_ROWS`). The
  rule is one sentence (`rule_sentence()`, the words of `single_choice_rule`, `approval_rule`
  and `condorcet_rule` in `macros/results.html`); its settings fold under "Modifier la règle", a
  `<details>` open when one of its fields is refused (`RULE_FIELDS`); a resolution's labels fold
  under "Modifier les intitulés" the same way. The blank vote has no field: `poll_input()`
  stores `blank_label` "Abstention" for a resolution and none ("Vote blanc") otherwise, so
  saving a draft of before gives it that label.

`POST …/polls/new` and `POST …/polls/{poll_id}/edit` take `EditorFields` (the raw form, `kind`
included) and the button pressed. Saving: `action=draft` (also the first, hidden, submit button,
so Enter never launches) or `action=launch` (in session: create or save, then
`polls.open_poll` with the optional `minutes`, one transaction; back to `#tab-polls`, where the
polls open now show it). Rendering without saving (status 200): `more` (five more option
rows), `change` (step 1 again, its cards as radio buttons `switch` of a form whose hidden fields
carry every value, `EditorFields.carried()`; the radio button of the card of the method typed
names that method, so a resolution comes back a resolution), `switch=<kind>` (step 2 of that
method, `switch_kind()`: title, text and timer kept, options kept between methods of free
options, set aside by a resolution in hidden `kept_label`/`kept_description` fields and given
back after it, the base and a threshold chosen kept between a resolution and free options when
the other form offers it, the defaults of the new method for the rest; "Passer au jugement
majoritaire" of a single choice and the forms of a card are such buttons). `poll_input()` builds a `PollInput`;
`polls.poll_problems()` returns every problem with its field, so the form comes back with
status 400, values kept, one message per field. Without JavaScript: fixed option rows plus the
`more` submit, links at step 1, submits for the rest. `editor.js` only enhances: it rewrites the
rule sentence from `data-rule-texts` (every sentence translated, `{option}`, `{base}`, `{share}`
and `{n}` left for it to fill, as the server does), keeps a resolution's preview in step,
adds or removes option rows in place, and sends the forms of a card with `fetch` (the `switch`
button added, `redirect: "manual"`), putting the `#poll-editor` of the answer in place of the
form and the focus back on the button pressed; a refusal or a redirection sends the form as
without script. Drafts: `POST …/polls/{poll_id}/move` (`direction`);
`poll_action()` brings a draft action back to `#drafts` by default. Code cards: `POST /manage/{id}/codes`
with `output=pdf` returns the card PDF (`no-store`); without it, the printable page. Codes exist
in clear only in that response.

## Projection screen

`agaura/web/screen/`, `services/screen.py`, `services/timer.py`, `templates/screen/`,
`static/{css,js}/screen.*`. No account: the secret link is the only credential.

Layout (FR-73): `screen-items` puts up to three cards (polls open, the result shown next to
them) in one row (`columns-1` to `columns-3`), four two by two; the strip of earlier questions
(`.screen-past.is-strip`) takes the height of its lines, a third of the stage at most; nothing in
a card shrinks (`.screen-card > * { flex-shrink: 0 }`), so a card that does not fit overflows
and `screen.js` lowers its type rather than squashing a clamped description; after the session
(`.screen-stage.is-closed`) the questions share the stage equally with the closing message.
`tests/web/e2e/test_screen.py::test_a_busy_stage_stays_readable` checks the fullest stage at
both resolutions. Closing times read to the second (`clock_time`).

| Route | Purpose |
|---|---|
| `GET /{session_id}/screen/{token}` | Full page (own layout on `base.html`); other ID spellings redirect (303) |
| `GET /{session_id}/screen/{token}/state` | Content (`screen/_state.html`) for refreshes; header `X-Server-Time` (ms) |
| `GET /{session_id}/screen/{token}/events` | SSE following `manage_channel(public_id)` |
| `POST /manage/{public_id}/screen-link` | New screen link; the open screen gets 404 at its next refresh, triggered at once by the console event |
| `POST /manage/{public_id}/screen/show` | A block of the screen shown or hidden from "Ce que la salle voit" (`part`, `shown`, `poll`); owner and right `screen` |
| `POST /manage/{public_id}/public-results` | `enabled=1`/`0`: public results link on/off; enabling an enabled link changes nothing |
| `POST /manage/{public_id}/polls/{poll_id}/screen` | `shown=1`/`0`: the result of a closed poll on the screen or off it (FR-75; `PollsSession`, so delegates with the polls right too); open, draft and cancelled polls are refused; asking for the current state changes nothing |
| `GET /manage/{public_id}/timer` | Chronometer card alone (`no-store`, flash cookie untouched), for `timer.js` |
| `POST /manage/{public_id}/timer/{start,pause,resume,reset,add,show}` | Chronometer: start takes `mode` (`countdown` or `up`), `duration` (seconds of a preset, or `custom` with `minutes`/`seconds`), `label`, `show`; show takes `shown` |

- Every request checks the token with `access.check_link_token` (constant time); a wrong,
  regenerated or deleted link gets the 404 page, and `screen.js` reloads into it.
- Responses: `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, `X-Robots-Tag: noindex`.
  Rendered with `templates.TemplateResponse`, not `render()`, so a screen open in the console's
  browser never consumes the flash cookie; `screen.js` fetches with `credentials: "omit"`.
- **Content.** The screen shows the title; attendees and quorum (in session); the status of
  entries (`screen.Entries`: open, locked, soon, ended); while entries are open, the address
  with the QR code (`…/{ID}#k={quoted password}`) and password, or the card instruction; the
  chronometer when shown (never after the session); the agenda with the current item
  highlighted, in a window (`agenda_window`; never after the session); and the questions put to
  the vote, most recently pushed first: open polls with options, turnout and countdown, then the
  others with "Vote clos" or "Vote annulé", or the verdict of a result shown. It never shows
  drafts, the result of an open or cancelled poll, cancellation reasons or anything about one
  voter. The password is decrypted only when shown.
- **Results, only when the tribune shows them** (FR-75). `polls.show_on_screen(db, owner,
  poll_id, shown)` sets `Poll.shown_on_screen` and `Poll.screen_shown_at` for a closed or
  published poll, independently of `shown_to_voters` (publishing and hiding from voters leave
  it alone; cancelling turns it off) and logs `poll.screen_shown` / `poll.screen_hidden` with
  the poll's id only. `ScreenView.results` (`ScreenResult`: `verdict`, `ballots`, `detail`)
  lists them, most recently shown first; `screen_view` keeps only closed and published polls,
  whatever the flag. The latest (`stage_result`) takes the main place, on a night card
  (`result_card`): the verdict in words (`verdict_text`; `screen.Verdict`: adopted, rejected,
  decided, tie "à trancher selon les statuts", none), the rule, and from `SCREEN_DETAIL_MIN`
  (10) ballots one row per option (votes, share, bar, merit profiles) and the counts; below,
  "Détail des voix non affiché : moins de 10 bulletins" and no figure at all, not even the
  number of ballots. Earlier results still shown keep one line (title and verdict pill) in the
  questions put to the vote (`past_listed` leaves the card's poll out). After the session the
  closing message keeps the stage and results keep their line.
- **Layout** (`ScreenView.hero`, `ScreenView.side`): one block takes the stage (open polls, with
  the latest result shown beside them in the same grid; else that result, with the past
  questions in a strip below; else join, chronometer, agenda, past questions, else a message);
  the others stack in a 30 % side column. The stage row is `minmax(0, 1fr)`, so a long result
  is fitted, never cut by the viewport. Root font size `min(2vw, 3.6vh)`; each `data-fit` box gets the largest
  `data-fit-level` (−6 to 6) at which nothing overflows; boxes take their size from the layout,
  never from their content, so the page never scrolls. How to join (`join_panel`, `layout`):
  on the stage, the QR code beside "Pour voter", the address, "Mot de passe", the password and
  the hint on the QR code; alone in the side column, the same under the QR code; beside other
  blocks of the side column, one row (`.is-row`): the QR code at the left of the address (the
  session ID on its own line) and the password, no hint, the box as tall as its content (40 %
  of the column at most), fitted first (`data-fit-first`) so the chronometer and the agenda are
  fitted in the rest; the agenda leaves it out of the blocks it shares the column with when it
  chooses its number of lines.
- **`screen.js`**: at most two refreshes per second (so a console action shows within a second, FR-73), polling every 5 s while SSE is down, a
  safety refresh every 30 s, DOM replaced only when the HTML changed. Countdowns run on the
  server clock. The chronometer is counted from `data-timer-duration` (empty: count up),
  `data-timer-elapsed` (whole seconds before the current run) and `data-timer-started` (ms;
  empty while stopped), with the arithmetic of `services/timer.py`. Full screen button, idle
  cursor hidden, `navigator.wakeLock`. No visible text in the script.
- **Chronometer** (`services/timer.py`): state in `Assembly.timer_*`; `state(assembly, now)`
  returns a `TimerState` (`display`, `overtime`, `paused`, `ready`). Allowed in preparation and
  in session; no audit entry.
- **Console providers**: `links_context` gives `links_screen_url`, `links_results_url` (None
  when disabled) and `links_screen_view`, the screen's own `ScreenView`; `timer_context` gives
  `timer_state`, `timer_presets`, `timer_now_ms`.
- **"Ce que la salle voit"** (`manage/_screen_recap.html`, next to the screen link; the owner's,
  and a delegate's holding `screen`, alone in its rubric, without the links): what the screen
  shows, in words, from `links_screen_view`: the header (title, attendees and quorum in
  session, the status of entries in the screen's words), then the blocks in the order of the
  stage (`hero`, then `side`): the polls open now with their turnout, the result on the main
  place (title, verdict as `verdict_text` words it, with or without the count of votes), the
  questions put to the vote with each earlier result still on its line, how to join, the
  chronometer, the agenda item, the message; then what the screen leaves out (chronometer
  hidden, joining hidden by the tribune or by the lock, agenda hidden, each closed poll's
  result not shown, `ScreenView.results_off`) and the rule that it never shows the result of an
  open or cancelled poll. It names a result only from `ScreenView.results`, so never one the
  screen does not show. Each block that can be hidden ends its line with a small form
  (`toggle`, `.recap-toggle`): `POST /manage/{id}/screen/show` with `part` (`result` and its
  `poll`, `timer`, `agenda`, `join`) and `shown`, a button "Masquer" or "Afficher" whose
  accessible name says which block ("Masquer : chronomètre"); the route
  (`links.show_on_screen`, `ScreenSession`) calls `polls.show_on_screen`, `timer.show` or
  `screen.show_part` (`assembly.screen_agenda_hidden`, `screen_join_hidden`, logged as
  `screen.<part>_shown`/`_hidden`) and comes back to `#screen-now`. A way to join hidden keeps
  the password out of `screen_view` and puts the address at the foot of the screen
  (`ScreenView.join_shown`). "Confier" in its head hands the right `screen` over. Its list
  (`_screen_recap_list.html`) is a live part (`screen-now`): it follows what other consoles,
  delegates, the chronometer or a poll's timer change, and its forms are live forms
  (`data-live-form`, `run_action(in_place=True)`: 204, then the live parts), each button with
  an id of its block (`screen-toggle-timer`, `screen-toggle-result-<poll id>`) so the focus
  stays on it; without script, a `<noscript>` hint says the list is as of the page's loading.
- **Icons on the screen**: the status of entries leads with an icon (door open, lock, calendar
  of the opening, check of the end; `.entries.has-icon`), the chronometer's label, the agenda's
  eyebrow and the state of each question put to the vote (lock, ban) too; the open polls carry
  `.screen-open`, their turnout bar in plum; the AGaura mark sits in the lower left corner
  (`.screen-brand`, outside `#screen`, which `screen.js` replaces), the stage keeping room for it
  when there is no foot.
  `timer.js`, loaded by `_timer.html`, counts on in the card, corrects the device clock and
  refetches the card on `agaura:console-refreshed`; it replaces only `[data-timer-status]`,
  never the start form.

## Results and exports

Console exports live in `agaura/web/manage/exports.py`; formats, digest and recount procedure
in [EXPORTS.md](EXPORTS.md). Exports of a poll answer 409 until voting on it has stopped: the
ballots of an open poll are never handed out (two exports during the vote would reveal each new
ballot). A trial vote (`Poll.trial`, created by the checklist) is no part of the record:
`services/exports.py` gives it no number and leaves it out of the session record (minutes,
session JSON, public results, the attendance readings of its opening and closing, the
console's attendance record), its own exports answer 409, and the console marks it with the
pill "Essai" (drafts, polls open now, commands, finished polls). Each download is logged (`export.*` actions, never the content); one started from another site's page (`Sec-Fetch-Site: cross-site`) is refused before anything is rendered or logged (`SameSiteOnly`). Message codes of this
area start with `export_`.

Every download of the console sits in Documents (D-H, `_exports.html`, owner only): the minutes
(PDF) and the complete data (JSON) of the session, then the table "Par vote", one row per poll
pushed in the session order (`exports_polls`, numbered as in the minutes and the file names; no
row for a draft or a trial vote), with its ballots (CSV) and complete data (JSON) once voting on
it has stopped and no link while it is open (those routes answer 409). Each finished poll of
Votes points to its row ("Exports de ce vote, dans Documents", `#exports-<id>`) instead of
carrying the links; the summary of Steering after the session keeps the two session files. The
code cards are the exception: shown once, when generated (FR-23), they stay in Accès et
présences, and Documents only names them, with the way there (codes mode). Each
"Télécharger" button names its file for a screen reader (a visually hidden end of label), and
the title of each row is its header cell, the context of its two links.

The public results page `GET /{session_id}/results/{token}` (`agaura/web/results/`) shows the
polls whose result was published at least once and the cancelled polls with their reason;
`no-store`, `noindex`, no live updates.

## Secrets in paths

Screen and public results tokens, like invitation, email verification, reset and email change tokens, travel
in URL paths. The production image runs uvicorn without access log, and Caddy replaces them
with `[redacted]` and drops the `Referer` header in every log line that describes a request:
the access log and the default logger, which records failed proxy requests (`deploy/Caddyfile`).
In development, the uvicorn access log in the terminal shows them.

## Tests

`tests/web/conftest.py` provides `app`, `client` (no redirect following), `world` (builds
sessions, voters and polls on the application's database), `owner` (verified account),
`admin` (client logged in as `owner`), `post(client, url, data)` (form with CSRF token) and
`make_account()`. Each area keeps its tests in `tests/web/<area>/`.
