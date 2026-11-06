# AGaura — Results, exports and independent recount

What the console exports, in which format, and how anyone can check a result from the JSON file
alone (SPEC FR-17, FR-51, FR-52, FR-60 to FR-63). Code: `agaura/exports/`,
`agaura/services/exports.py`, `agaura/web/manage/exports.py`, `agaura/web/results/`,
`scripts/recount.py`.

## Routes

| Route | Content | Name of the file |
|---|---|---|
| `GET /manage/{ID}/polls/{poll_id}/ballots.csv` | Anonymous ballots of one poll, for a spreadsheet | `agaura-{ID}-vote-{n}-bulletins.csv` |
| `GET /manage/{ID}/polls/{poll_id}/export.json` | One poll: definition, ballots, stored result, digest | `agaura-{ID}-vote-{n}.json` |
| `GET /manage/{ID}/export.json` | Every finished poll of the session, same shape | `agaura-{ID}-session.json` |
| `GET /manage/{ID}/minutes.pdf` | PDF minutes of the session | `agaura-{ID}-proces-verbal.pdf` |
| `GET /{ID}/results/{token}` | Public results page (secret link, FR-17) | — |

`{n}` is the number of the poll in the session order (the order polls were pushed), the same
number as in the console and the minutes. File names come from the French catalog.

**In the console**, every download sits in the rubric Documents: the minutes and the session
file, then the table "Par vote", one row per poll pushed with its CSV and JSON once voting on it
has stopped (no link while it is open, no row for a draft or a trial vote). A finished poll of
Votes leads to its row ("Exports de ce vote, dans Documents"). The code cards are not among
them: shown once, when generated (SPEC FR-23), they are downloaded from Accès et présences.

**Trial vote.** The checklist of Séance, before the opening, may create a trial vote ("Tout le
monde est-il connecté ?", `Poll.trial`, SPEC FR-91) to test the room's network. It runs like any poll, but
no document carries it: it gets no number (the others are numbered without it), the minutes,
the session export and the public results page leave it out with the attendance readings of
its opening and closing, and its own exports answer 409 ("Un vote d'essai teste seulement le
réseau de la salle…"). An export therefore recounts exactly the polls of the record.

- The console routes are owner only: another account gets 404, an anonymous visitor goes to the
  login page. A poll can be exported once voting on it has stopped (closed, published or
  cancelled); a draft, an open poll or a trial vote answers 409. Session exports always answer
  200, whatever the state of the session.
- Every download is recorded in the session log (`export.ballots_csv`, `export.poll_json`,
  `export.session_json`, `export.minutes_pdf`, with the poll id and title for poll exports),
  never with any content.
- Responses carry `Content-Disposition: attachment` and `Cache-Control: no-store`.
- **PDF limits** (minutes and code cards alike, SPEC §9.3). One PDF is rendered at a time in the
  process; a request waits at most 3 s for its turn, then gets `503` with `Retry-After: 10`
  ("Serveur occupé"). An account may ask for 10 PDFs in 10 minutes, busy answers included, then
  gets `429` ("Trop de documents"). The code cards reserve the renderer before generating the
  batch, so a refused request creates no code.
- **Secrecy.** No export ever contains a credential, a code, a device token, a timestamp per
  ballot or anything linking a ballot to a person. The ballots of an open poll are never handed
  out: comparing two exports taken while voting goes on would reveal each new ballot. The session
  exports therefore leave open polls (and drafts) out; the minutes mention an open poll without
  any figure.

## Public results page

`/{ID}/results/{token}` exists only while the public link is enabled in the console; a wrong,
regenerated or disabled token answers 404, exactly like an unknown session. The page lists, in
the session order, the polls whose result was published at least once (status `published`, even
if the result was hidden since) and the cancelled polls with their reason. Drafts, open polls,
closed polls whose result was never published and trial votes never appear. It shows the same result blocks
as the voter page, the turnout and the SHA-256 digest; no voter data. It is never cached
(`Cache-Control: no-store`), not indexed (`X-Robots-Tag: noindex`), and refreshed by hand.

## CSV (FR-60)

Dialect chosen for French spreadsheets, which open the file with a double click:

- separator `;` (Excel in French expects it, the comma being the decimal separator);
- UTF-8 with a byte order mark (without it, Excel reads accented letters as Latin-1);
- CRLF line ends; double quotes only around cells that need them, `""` inside quoted cells.

LibreOffice and Numbers detect all of this by themselves. With Python:
`csv.reader(open(path, encoding="utf-8-sig", newline=""), delimiter=";")`.

One header row, then **one row per ballot in a random order** drawn from the operating system's
generator (`secrets.SystemRandom`), so that the order reveals nothing about arrival times; two
downloads of the same poll list the rows in different orders. Columns: one per option (header:
the option label, in the poll's order), then one for blank ballots (header: the blank label,
"Vote blanc" by default).

| Method | Option cells | Blank column |
|---|---|---|
| Single choice | `1` under the chosen option, `0` elsewhere | `1` for a blank ballot, else `0` |
| Approval | `1` under each approved option, `0` elsewhere | same |
| Majority Judgment | the grade given to each option, as its label ("Très bien" … "À rejeter"); empty for a blank ballot | same |
| Condorcet | the rank given to each option, `1` for the first choice, as stored (dense ranks, the same for options put level, the unranked options at the last rank); empty for a blank ballot | same |

Summing a column gives the votes (single choice) or approvals of an option. A cell that a
spreadsheet would read as a formula (starting with `=`, `+`, `-`, `@`, a tab, a carriage return,
a line feed, or their full-width forms) gets a leading apostrophe, so an option labelled
`=HYPERLINK(…)` stays plain text.

## JSON (FR-61, FR-63)

UTF-8, indented for reading. The poll export and the session export share one shape; the poll
export lists one poll.

```json
{
  "format": "agaura-export",
  "version": 1,
  "generated_at": "2026-06-20T18:30:00+00:00",
  "session": { … },
  "polls": [ { … }, … ]
}
```

Times are UTC, ISO 8601, to the second. `null` stands for a missing value.

**Compatibility.** Fields may be added within a version, so readers must ignore the fields they
do not know; `version` changes only when a field is removed or changes meaning. The `tribune`,
`agenda`, `attendance` and `notes` fields of `session`, and `agenda_item` of `polls[]`, were
added this way, within version 1: files made before them lack these fields, and every other
field is unchanged (the digests and the recount procedure use neither `session` nor
`agenda_item`).

### `session`

| Field | Meaning |
|---|---|
| `id` | Session ID (6 characters) |
| `title`, `date` (`YYYY-MM-DD`), `place` | Session details |
| `status` | `preparation`, `in_session` or `closed` |
| `access_mode` | `password` (one vote per device) or `codes` (one printed code per person) |
| `registered_members` | Registered members, if given |
| `quorum_rule` | `"50"` (attendees) or `"1/4"` (share of registered members), if given |
| `quorum_required` | Attendees needed for the quorum, `null` when unknown |
| `attendees` | Voter accesses in use when the file was generated (FR-26) |
| `headcount` | People counted in the room by the tribune (FR-28), if entered |
| `opened_at`, `closed_at` | Session opening and closing times |
| `tribune` | The people running the session, in the order of the minutes: `[{"name", "role"}]`, `role` being the function typed freely ("Présidente de séance"), `null` when none; `[]` when the tribune was not filled in |
| `agenda` | The agenda, in order: `[{"title"}]`; `[]` when there is none. Which item was under discussion is not exported |
| `attendance` | Attendance readings (FR-28), in time order: see below |
| `notes` | Session notes of the tribune, in time order, **without the internal ones**: see below |

`session.attendance[]`: one reading each time the session opened or closed, the headcount was
saved, or a poll was pushed or closed (by hand or by its timer); never when someone joined or
voted. Aggregated counts only, nothing about one voter.

| Field | Meaning |
|---|---|
| `at` | Time of the reading |
| `event` | `session_opened`, `headcount_set`, `poll_opened`, `poll_closed` or `session_closed` |
| `poll_number` | Number of the poll pushed or closed (as in `polls[].number`), else `null` |
| `headcount` | People counted in the room as last entered by the tribune at that moment (for `headcount_set`: the number just entered, `null` when it was removed); `null` when none |
| `attendees` | Voter accesses in use at that moment, as `attendees` above; `null` in readings recorded before this field existed |

The readings come from the session log (`agaura audit verify` checks it), where the entries of
those actions carry the two counts.

`session.notes[]`: the notes written by the tribune during the session (never by voters). Notes
marked internal in the console are left out, since this file may be shared for a recount.

| Field | Meaning |
|---|---|
| `at` | When the note was written |
| `updated_at` | Last correction, `null` if never corrected |
| `agenda_item` | Position (1, 2, …) in `session.agenda` of the item the note belongs to, `null` for none |
| `text` | Plain text, line breaks as `\n` (at most 5,000 characters) |

### `polls[]`

In the session order. The session export lists every poll that is closed, published or
cancelled, except a trial vote.

| Field | Meaning |
|---|---|
| `number` | Number of the poll in the session order (1, 2, 3…) |
| `id` | Internal poll id (the session log refers to it) |
| `status` | `closed`, `published` (result shown at least once) or `cancelled` |
| `opened_at`, `closed_at` | When voting opened and stopped (for the poll, never per ballot) |
| `cancel_reason` | Reason given when cancelling, else `null` |
| `turnout` | Participation records: how many credentials voted; equals the number of ballots |
| `definition` | The poll as voters saw it: **first half of the canonical form** (below) |
| `ballots` | The ballots, sorted by id: **second half of the canonical form** |
| `result` | What the server stored when the poll closed, or `null` for a poll cancelled while open |
| `agenda_item` | Position in `session.agenda` of the item under discussion when the poll was pushed, `null` for none (or a poll pushed before this was recorded) |

`definition`:

| Field | Meaning |
|---|---|
| `title`, `description` | Question and explanatory text |
| `method` | `single_choice`, `approval`, `majority_judgment` or `condorcet` |
| `params` | Method parameters, see the docstrings of `agaura/voting/single_choice.py`, `approval.py`, `majority_judgment.py`, `condorcet.py` (`adopt_option`, `base`, `threshold`, `seats`, `max_choices`, `min_share`, `grade_count`; none for `condorcet`) |
| `blank_label` | Label of the blank choice, `null` for the default ("Vote blanc") |
| `options[]` | `{"id", "label", "description"}`; `id` is the option's position (1, 2, 3…), the identifier used by ballots and results |

`ballots[]`: `{"id", "is_blank", "content"}`. `id` is a random UUID (32 hexadecimal digits),
drawn when the ballot was cast: it carries no order and no link to anyone, which is why sorting
by it reveals nothing. `content` is `null` for a blank ballot, otherwise:

| Method | `content` |
|---|---|
| Single choice | `{"choice": 2}` |
| Approval | `{"approved": [1, 3]}` (sorted, at least one option) |
| Majority Judgment | `{"grades": {"1": 0, "2": 3}}`, every option, grade `0` the best ("Très bien") to `grade_count - 1` the worst ("À rejeter"; ungraded options get it) |
| Condorcet | `{"ranks": {"1": 2, "2": 1, "3": 2}}`, every option, rank `1` the first choice; dense ranks (no gap), the same for options put level, the options left unranked at the last rank |

`result`:

| Field | Meaning |
|---|---|
| `ballots_sha256` | Digest computed at closing (also written in the session log entry `poll.closed`) |
| `computed_at` | Closing time, when the tally ran |
| `published_at` | First publication of the result, else `null` |
| `data` | `{"attendees", "method", "counts", "outcome"}`: `attendees` is the number of voter accesses in use at closing (the base of single choice polls whose `base` is `attendees`), `counts` is `{"cast", "blank", "valid"}`, `outcome` the method's result (`agaura.voting.to_jsonable`: shares as exact `"numerator/denominator"` strings, rankings as lists of tied groups, `status` among `decided`, `tie`, `threshold_not_met`, `seats_unfilled`, `no_valid_votes`) |

The `outcome` of a Condorcet poll also holds the duels: `pairwise[i][j]` counts the valid
ballots that rank option `options[i]` above option `options[j]` (the order of `outcome.options`,
that of the poll), `strongest_paths[i][j]` the strength of the strongest chain of duels won from
one to the other (Schulze, *winning votes*); `options[]` gives each option's duels `wins` and
`losses`, and `condorcet_winner` the option that wins every duel, or `null` (SPEC §6.5).

A cancelled poll keeps its ballots, and its stored result when it was cancelled after closing;
no result of a cancelled poll is ever announced.

## Digest (FR-63)

The digest of a poll is the SHA-256, in lowercase hexadecimal, of the **canonical JSON** of

```json
{"poll": <definition>, "ballots": <ballots>}
```

where `<definition>` and `<ballots>` are exactly the `definition` and `ballots` fields of the
export (ballots sorted by `id`). Canonical JSON means:

- object keys sorted by Unicode code point, at every level (so `"10"` comes before `"2"` among
  Majority Judgment grades and Condorcet ranks);
- no whitespace at all: `,` and `:` separators;
- non-ASCII characters written as themselves, not as `\u` escapes; `"` and `\` escaped with a
  backslash, control characters as `\n`, `\r`, `\t`, `\b`, `\f` or `\u00XX`; `/` not escaped;
- the result encoded in UTF-8.

That is Python's `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.

### Worked example

A resolution with three ballots (For, blank, For):

```json
{"ballots":[{"content":{"choice":1},"id":"0f8e3c2a9b7d4e61a5c3f1d2b4e6a8c0","is_blank":false},{"content":null,"id":"5b1d7e9f3a2c4b68b0d2e4f6a8c1e3f5","is_blank":true},{"content":{"choice":1},"id":"c4a2e6f8b1d3459788a6c4e2f0b9d7a5","is_blank":false}],"poll":{"blank_label":"Abstention","description":null,"method":"single_choice","options":[{"description":null,"id":1,"label":"Pour"},{"description":null,"id":2,"label":"Contre"}],"params":{"adopt_option":1},"title":"Approbation des comptes"}}
```

```sh
printf '%s' '<the line above>' | sha256sum
# 4cc1b7916a4de0672ba02880c068891d9ceed25e43a26faeaa9de4c5d43b8eb8
```

Recounting it with 3 attendees gives `counts` `{"cast": 3, "blank": 1, "valid": 2}`, For with
2 votes and a share of `"1/1"` of valid votes, `"winner": 1`, `"adopted": true`.

## Recount procedure

```sh
uv run python scripts/recount.py agaura-K7M4QX-session.json
```

For every poll of the file, the script, which uses only the standard library and the pure
tallying engine `agaura.voting` (no database, no application code, its own implementation of
the canonical form):

1. checks the ballots: sorted by id, no duplicate id, `is_blank` true exactly when `content` is
   `null`, as many ballots as `turnout`;
2. recomputes the digest from `definition` and `ballots` and compares it with
   `result.ballots_sha256`;
3. tallies the ballots again (`agaura.voting.tally`, with `result.data.attendees` as the number
   of attendees) and compares `{"attendees", **outcome}` with `result.data`, field by field.

It prints one line per poll (`OK` or `MISMATCH` with the reasons) and exits with status 0 when
everything matches, 1 on any mismatch, 2 when the file cannot be read as an AGaura export. A
poll cancelled while open has no stored result: only its digest is printed.

To check without this script, recompute the digest as above with any JSON library that can sort
keys, and compare it with the digest printed in the minutes or in the session log; then count
the ballots following SPEC §6.

The automated test `tests/web/manage/test_exports.py::test_recount_through_the_web_export`
exports a session with three methods, blank ballots and ties through the web route and recounts
it; `tests/exports/test_recount.py` recounts a Condorcet poll with a cycle and checks that
tampering is detected.

## PDF minutes (FR-62)

A4, rendered by WeasyPrint from `agaura/templates/exports/minutes.html` (inline CSS, nothing
fetched, fonts DejaVu in the Docker image), after the read transaction. Content:

- header: title, date, place, session ID and status, generation time (Paris time);
- session facts: the tribune when filled in (each name with its function, in order), opening
  and closing times, access mode, attendees, registered members, quorum required and whether it
  is reached, headcount if entered, number of polls;
- the attendance record: a table of the readings (time, event, headcount, connections) in time
  order, when it says more than the opening of the session; the readings of a trial vote are
  left out;
- the agenda as a numbered list, when there is one;
- the course of the session ("Déroulé de la séance"): for each agenda item in order, the notes
  of the tribune (never the internal ones) and the polls pushed while it was under discussion
  (title, short outcome, reference to the detailed poll with its page), in time order; then
  notes and polls outside any item; without an agenda, one chronological feed. Shown when there
  is a note, or an agenda and polls. A note or a poll recorded under an item that was later
  renamed or moved follows the item's title, else its position;
- every poll pushed except a trial vote, in the session order: number, question, explanatory
  text, method, opening and closing times, then
  - counts (ballots cast, blank, valid) and turnout against the attendees at closing; blank
    ballots go by the words of the ballot's blank choice ("Abstention", or "Vote blanc"),
  - the rule applied (threshold and base, seats, minimum share),
  - the outcome (adopted or rejected, winner, elected), ties reported as such,
  - a result table per method; for Majority Judgment the merit profile (votes per grade, majority
    grade shaded, share above and below, stacked bar); for Condorcet the ranking with the duels
    won and lost, whether an option wins every duel, the matrix of duels in the order of the
    ranking (duels won in bold, shaded) and, after a cycle, the matrix of the strongest chains,
  - the SHA-256 digest;
- cancelled polls with their reason and the number of ballots kept, without any result; open
  polls as "still open", without any figure;
- a note on digests and recount;
- the anchor of the session log ("Empreinte du journal de la session", below);
- no signature lines.

Page numbers (`n / total`), the session title and ID repeat in the margins.

### Anchor of the session log

The minutes end with the first 16 hexadecimal digits of the hash of the session's most recent
entry in the audit log when the document was generated (`audit.session_anchor`; the download
itself is logged just after, so the next copy shows another anchor). It says nothing about
ballots or results: it is not a digest of the poll data and plays no part in the recount.

The log is hash-chained with a key derived from `AGAURA_SECRET_KEY` (HMAC-SHA256, SPEC FR-83):
each entry's hash depends on that entry and on every entry before it, across the whole platform.
A copy of the minutes kept away from the server therefore pins the log as it was: the operator
runs

```sh
docker compose exec app agaura audit verify --anchor 3f2a9c0e1b7d4e61
```

which checks the keyed chain, then that an entry still has a hash starting with these digits
(exit status 0, or 1 with `NOT FOUND`). A missing anchor means entries up to it were removed or
rewritten since, even by someone holding the key, or that the minutes come from another server.
