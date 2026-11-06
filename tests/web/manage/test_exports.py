"""Console exports (FR-51, FR-52, FR-60 to FR-63): routes, headers, audit, recount."""

from __future__ import annotations

import csv
import io
import json
import re
from html import unescape
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select

from agaura.exports import recount
from agaura.models import AccessMode, AuditEntry, Result, User
from agaura.services import polls
from agaura.services.polls import PollInput
from tests.conftest import World
from tests.exports.conftest import Built, build_session, vote_all
from tests.exports.test_trial import build_trial_session
from tests.web.conftest import make_account, post
from tests.web.manage.test_delegates import ALL_RIGHTS, card_of, make_link, open_link
from tests.web.manage.test_layout import rubrics, text_of


@pytest.fixture
def built(world: World, owner: User) -> Built:
    return build_session(world, owner)


def poll_url(built: Built, key: str, name: str) -> str:
    return f"/manage/{built.public_id}/polls/{built.polls[key]}/{name}"


def export_actions(world: World) -> list[tuple[str, dict]]:
    with world.database.read() as db:
        entries = db.scalars(
            select(AuditEntry).where(AuditEntry.action.like("export.%")).order_by(AuditEntry.id)
        ).all()
        return [(entry.action, entry.details) for entry in entries]


def assert_attachment(response: Response, media_type: str, filename: str) -> None:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(media_type)
    disposition = response.headers["content-disposition"]
    assert disposition.startswith(f'attachment; filename="{filename}"'), disposition
    assert response.headers["cache-control"] == "no-store"


def test_ballots_csv(admin: TestClient, world: World, built: Built) -> None:
    response = admin.get(poll_url(built, "approval", "ballots.csv"))
    name = f"agaura-{built.public_id}-vote-2-bulletins.csv"
    assert_attachment(response, "text/csv; charset=utf-8", name)
    assert response.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
    assert rows[0] == ["Alice", "Bruno", "Chloé", "David", "Vote blanc"] and len(rows) == 7


def test_poll_json(admin: TestClient, world: World, built: Built) -> None:
    response = admin.get(poll_url(built, "majority_judgment", "export.json"))
    assert_attachment(response, "application/json", f"agaura-{built.public_id}-vote-3.json")
    document = response.json()
    assert document["format"] == "agaura-export"
    assert [entry["number"] for entry in document["polls"]] == [3]


def test_session_json_and_minutes(admin: TestClient, built: Built) -> None:
    response = admin.get(f"/manage/{built.public_id}/export.json")
    assert_attachment(response, "application/json", f"agaura-{built.public_id}-session.json")
    assert len(response.json()["polls"]) == 6
    pdf = admin.get(f"/manage/{built.public_id}/minutes.pdf")
    assert_attachment(pdf, "application/pdf", f"agaura-{built.public_id}-proces-verbal.pdf")
    assert pdf.content.startswith(b"%PDF-")


@pytest.mark.parametrize("site", ["same-origin", "same-site", "none"])
def test_downloads_started_here_go_through(admin: TestClient, built: Built, site: str) -> None:
    """``none``: typed in the address bar or opened from a bookmark."""
    response = admin.get(f"/manage/{built.public_id}/export.json", headers={"Sec-Fetch-Site": site})
    assert response.status_code == 200


def test_downloads_started_from_another_site_are_refused(
    admin: TestClient, world: World, built: Built
) -> None:
    """SameSite=Lax sends the session cookie with a navigation from anywhere: another site's
    page could spend the PDF budget (10 renders in 10 minutes) and fill the session log."""
    before = export_actions(world)
    for path in (
        f"/manage/{built.public_id}/minutes.pdf",
        f"/manage/{built.public_id}/export.json",
        poll_url(built, "resolution", "export.json"),
        poll_url(built, "resolution", "ballots.csv"),
    ):
        response = admin.get(path, headers={"Sec-Fetch-Site": "cross-site"})
        assert response.status_code == 403, path
        assert not response.headers.get("content-disposition"), path
    assert export_actions(world) == before  # nothing logged


def test_an_id_past_the_integer_range_is_no_error_500(admin: TestClient, built: Built) -> None:
    for path in (
        f"/manage/{built.public_id}/polls/{2**63}/export.json",
        f"/manage/{built.public_id}/polls/{'9' * 40}/ballots.csv",
    ):
        assert admin.get(path).status_code == 422, path
    response = post(admin, f"/manage/{built.public_id}/polls/{2**63}/close", {})
    assert response.status_code == 422


def test_session_exports_work_in_preparation(admin: TestClient, world: World, owner: User) -> None:
    public_id = world.assembly(owner, in_session=False)
    world.poll(owner, public_id, open_it=False)
    document = admin.get(f"/manage/{public_id}/export.json").json()
    assert document["session"]["status"] == "preparation" and document["polls"] == []
    assert admin.get(f"/manage/{public_id}/minutes.pdf").content.startswith(b"%PDF-")


@pytest.mark.parametrize("name", ["ballots.csv", "export.json"])
def test_unfinished_polls_cannot_be_exported(
    admin: TestClient, world: World, owner: User, name: str
) -> None:
    public_id = world.assembly(owner)
    open_id = world.poll(owner, public_id)
    draft_id = world.poll(owner, public_id, open_it=False)
    for poll_id in (open_id, draft_id):
        response = admin.get(f"/manage/{public_id}/polls/{poll_id}/{name}")
        assert response.status_code == 409
        assert "Ce vote pourra être exporté une fois le scrutin terminé." in response.text
        assert "attachment" not in response.headers.get("content-disposition", "")
    assert export_actions(world) == []


def test_poll_of_another_session_or_account(admin: TestClient, world: World, owner: User) -> None:
    built = build_session(world, owner)
    other = world.assembly(owner)
    url = f"/manage/{other}/polls/{built.polls['resolution']}/ballots.csv"
    assert admin.get(url).status_code == 404
    stranger = make_account(world, "stranger@example.org")
    foreign = build_session(world, stranger)
    for url in (
        f"/manage/{built.public_id}/polls/{foreign.polls['resolution']}/export.json",
        f"/manage/{foreign.public_id}/minutes.pdf",
        f"/manage/{foreign.public_id}/export.json",
    ):
        assert admin.get(url).status_code == 404, url
    assert export_actions(world) == []


def test_exports_are_logged_without_content(admin: TestClient, world: World, built: Built) -> None:
    admin.get(poll_url(built, "resolution", "ballots.csv"))
    admin.get(poll_url(built, "resolution", "export.json"))
    admin.get(f"/manage/{built.public_id}/export.json")
    admin.get(f"/manage/{built.public_id}/minutes.pdf")
    details = {"poll_id": built.polls["resolution"], "title": "Approbation des comptes"}
    assert export_actions(world) == [
        ("export.ballots_csv", details),
        ("export.poll_json", details),
        ("export.session_json", {}),
        ("export.minutes_pdf", {}),
    ]
    log = admin.get(f"/manage/{built.public_id}").text
    for label in (
        "Bulletins exportés (CSV)",
        "Vote exporté (JSON)",
        "Session exportée (JSON)",
        "Procès-verbal téléchargé (PDF)",
    ):
        assert label in log


def test_recount_through_the_web_export(
    admin: TestClient, world: World, built: Built, tmp_path: Path
) -> None:
    """FR-63: the three methods, blank ballots and ties, exported and recounted from the file."""
    path = tmp_path / "session.json"
    path.write_bytes(admin.get(f"/manage/{built.public_id}/export.json").content)
    out = io.StringIO()
    assert recount.main([str(path)], out=out) == 0, out.getvalue()
    document = json.loads(path.read_text())
    with world.database.read() as db:
        stored = {row.poll_id: row for row in db.scalars(select(Result))}
    methods = set()
    for entry in document["polls"]:
        if entry["result"] is None:
            continue
        methods.add(entry["definition"]["method"])
        attendees = entry["result"]["data"]["attendees"]
        assert recount.tally(entry, attendees) == stored[entry["id"]].data
        assert recount.ballots_digest(entry) == stored[entry["id"]].ballots_sha256
    assert methods == {"single_choice", "approval", "majority_judgment"}
    statuses = {
        entry["result"]["data"]["outcome"]["status"]
        for entry in document["polls"]
        if entry["result"]
    }
    assert "tie" in statuses
    # Each poll export recounts on its own too.
    for key in ("resolution", "approval", "majority_judgment", "cancelled_open"):
        path.write_bytes(admin.get(poll_url(built, key, "export.json")).content)
        assert recount.main([str(path)], out=io.StringIO()) == 0


def test_console_shows_results_turnout_and_exports(admin: TestClient, built: Built) -> None:
    page = admin.get(f"/manage/{built.public_id}").text
    for text in (
        "Votes terminés",
        "N°\u00a01",
        "Participation",
        "6 / 6",
        "100,0 % des présents",
        "SHA-256",
        "Égalité entre Bruno, Chloé\u00a0: appliquez vos statuts",
        "Annulé\u00a0: Vote à refaire",
        "3 bulletins conservés, non dépouillés.",
        poll_url(built, "approval", "ballots.csv"),
        poll_url(built, "cancelled_open", "export.json"),
        f"/manage/{built.public_id}/minutes.pdf",
        f"/manage/{built.public_id}/export.json",
        'class="merit"',
    ):
        assert text in page, text
    assert " style=" not in page


def test_results_stay_after_the_session_is_closed(
    admin: TestClient, world: World, owner: User
) -> None:
    """FR-52: the closed session keeps its results and exports; copying is no longer offered."""
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, PollInput("Lieu", "single_choice", ["A", "B", "C"]))
    vote_all(world, public_id, world.voters(owner, public_id, 3), poll_id, [{"choice": 2}] * 3)
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
    post(admin, f"/manage/{public_id}/close")
    page = admin.get(f"/manage/{public_id}").text
    assert "Résultat\u00a0: B" in page and f"/polls/{poll_id}/ballots.csv" in page
    assert "Copier en nouveau brouillon" not in page
    assert admin.get(f"/manage/{public_id}/polls/{poll_id}/ballots.csv").status_code == 200


def test_a_trial_vote_has_no_export(admin: TestClient, world: World, owner: User) -> None:
    """D-I: the trial vote's own exports answer 409; its finished poll says why, without a
    number or download links; the summary does not count it."""
    public_id, trial, real = build_trial_session(world, owner)
    for name in ("ballots.csv", "export.json"):
        response = admin.get(f"/manage/{public_id}/polls/{trial}/{name}")
        assert response.status_code == 409, name
        assert "Un vote d&#39;essai teste seulement le réseau de la salle" in response.text
        assert admin.get(f"/manage/{public_id}/polls/{real}/{name}").status_code == 200
    page = admin.get(f"/manage/{public_id}").text
    start = page.index(f'id="poll-{trial}"')
    end = page.find('<details class="finished-poll', start)  # the next tile, or the card's end
    finished = page[start : end if end != -1 else page.index("</section>", start)]
    assert '<span class="badge neutral">Essai</span>' in finished
    assert "ballots.csv" not in finished and "N° " not in finished
    assert "Vote d'essai\u00a0: absent du procès-verbal et des exports." in unescape(finished)
    with world.database.write() as db:
        from agaura.services import assemblies

        assemblies.close_assembly(db, owner, public_id)
    summary = admin.get(f"/manage/{public_id}").text
    summary = summary[
        summary.index('id="summary"') : summary.index("</section>", summary.index('id="summary"'))
    ]
    assert "Votes tenus</dt><dd>1</dd>" in summary


def documents_of(client: TestClient, public_id: str) -> str:
    return rubrics(client.get(f"/manage/{public_id}").text)["tab-documents"]


def test_every_download_sits_in_documents(admin: TestClient, built: Built) -> None:
    """D-H: the minutes, the session data and each poll's CSV and JSON are in Documents, in the
    order of the session, numbered as in the minutes; a finished poll of Votes points to its
    row instead of carrying the links."""
    page = admin.get(f"/manage/{built.public_id}").text
    documents = rubrics(page)["tab-documents"]
    for path in ("minutes.pdf", "export.json"):
        assert f'href="/manage/{built.public_id}/{path}" download>' in documents
    # Each "Télécharger" names its file for a screen reader.
    assert 'Télécharger<span class="visually-hidden">, Procès-verbal (PDF)</span>' in documents
    pattern = r'<tr id="exports-(\d+)" role="row">\s*<td class="nowrap" role="cell">N°\s(\d+)<'
    rows = re.findall(pattern, documents)
    order = ["resolution", "approval", "majority_judgment", "cancelled_closed"]
    order += ["cancelled_open", "unpublished"]
    assert rows == [(str(built.polls[key]), str(n)) for n, key in enumerate(order, start=1)]
    for key in order:  # cancelled polls too: voting on them has stopped
        for name in ("ballots.csv", "export.json"):
            assert f'href="{poll_url(built, key, name)}" download>' in documents
    assert f"/polls/{built.polls['draft']}/" not in documents  # a draft has no row
    assert "Annulé" in text_of(card_of(documents, f"exports-{built.polls['cancelled_open']}"))
    # Votes keeps no download link: each finished poll leads to its row in Documents.
    votes = rubrics(page)["tab-polls"]
    assert "ballots.csv" not in votes and "export.json" not in votes
    for key in order:
        assert f'<a class="to-home" href="#exports-{built.polls[key]}">' in votes
    assert "Exports de ce vote, dans Documents" in text_of(votes)
    # The help no longer sends to Votes for the spreadsheet.
    assert "depuis le vote terminé dans Votes" not in page


def test_an_open_poll_has_no_export_link(admin: TestClient, world: World, owner: User) -> None:
    """Its exports answer 409 while it is open: Documents lists it without a link, then with
    its links once it is closed."""
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id)
    world.poll(owner, public_id, open_it=False)  # a draft: no row at all
    documents = documents_of(admin, public_id)
    row = card_of(documents, f"exports-{poll_id}")
    assert "Vote en cours\u00a0: fichiers à la clôture" in text_of(row)
    assert "ballots.csv" not in documents and "/polls/" not in documents
    assert admin.get(f"/manage/{public_id}/polls/{poll_id}/ballots.csv").status_code == 409
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
    documents = documents_of(admin, public_id)
    assert f"/manage/{public_id}/polls/{poll_id}/ballots.csv" in documents
    assert "Vote en cours\u00a0: fichiers à la clôture" not in text_of(documents)


def test_documents_point_to_the_code_cards(admin: TestClient, world: World, owner: User) -> None:
    """The code cards are shown once, when generated (FR-23): Documents names them and leads
    to Access, without a download of its own; only in codes mode."""
    public_id = world.assembly(owner, mode=AccessMode.CODES)
    documents = documents_of(admin, public_id)
    assert "Cartes de codes (PDF)" in text_of(documents)
    assert '<a class="to-home" href="#codes">' in documents and "/codes" not in documents
    assert "Aucun vote mis aux voix pour l'instant." in text_of(documents)
    assert "Cartes de codes" not in text_of(documents_of(admin, world.assembly(owner)))


def test_a_delegate_gets_no_documents(app: FastAPI, world: World, owner: User) -> None:
    """FR-92: the documents and exports are the owner's; a delegate with every right sees the
    finished polls without the way to their exports."""
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id)
    with world.database.write() as db:
        polls.close_poll(db, owner, poll_id)
    _, token = make_link(world, owner, public_id, ALL_RIGHTS)
    page = open_link(app, public_id, token).get(f"/manage/{public_id}").text
    assert 'id="tab-documents"' not in page and "#exports-" not in page
    assert f'id="poll-{poll_id}"' in page and "ballots.csv" not in page
