"""Take the screenshots of the README (``docs/screenshots/``) from a throwaway database: the
tribune on a computer, voters and a delegate on phones, the projection screen and the minutes.

    uv run python scripts/screenshots.py
    uv run python scripts/screenshots.py --out /tmp/shots

Needs Playwright's Chromium (``uv run playwright install chromium``) and, for the page of the
minutes, ``pdftoppm`` (poppler-utils); ``pngquant`` shrinks the images when it is installed.
The browser reaches the local server under the name ``agaura.org``, over HTTPS with a throwaway
certificate, so the addresses on the pages read as in production. Development only: the example
content is French sample data, not interface text.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from playwright.sync_api import Browser, BrowserContext, Page, expect, sync_playwright
from sqlalchemy.orm import Session

from agaura.config import Settings
from agaura.db import Database
from agaura.exports import minutes
from agaura.main import create_app
from agaura.models import User, utcnow
from agaura.security import Keyring
from agaura.services import (
    access,
    accounts,
    assemblies,
    ballots,
    delegates,
    exports,
    meeting,
    notes,
    polls,
    timer,
)
from agaura.services.assemblies import AssemblyInput
from agaura.services.polls import PollInput

ROOT = Path(__file__).resolve().parent.parent
HOST = "agaura.org"
BASE = f"https://{HOST}"
EMAIL = "tribune@example.org"
PASSWORD = "screenshots-password-2026"  # noqa: S105 (throwaway account of a temporary database)

# Viewports in CSS pixels, and the scale that gives the width of the image: 1600 px for the
# computer and the projection screen, 720 px for a phone.
DESKTOP = {"viewport": {"width": 1280, "height": 800}, "device_scale_factor": 1.25}
NARROW = {"width": 1040, "height": 780}
SCREEN = {"viewport": {"width": 1280, "height": 720}, "device_scale_factor": 1.25}
PHONE = "Pixel 7"
PHONE_SIZE = {"viewport": {"width": 412, "height": 800}, "device_scale_factor": 1.75}
MINUTES_DPI = 110
MINUTES_PAGES = {2: "minutes-course", 3: "minutes-polls"}


# How the simulated voters answer: For, Against or blank; two names at most; one grade each.
def resolution(rng: random.Random) -> Mapping[str, Any] | None:
    return rng.choices([{"choice": 1}, {"choice": 2}, None], weights=[78, 12, 10])[0]


def grades(rng: random.Random) -> Mapping[str, Any]:
    centres = {1: 0.8, 2: 2.6, 3: 1.9}
    return {
        "grades": {
            option: min(5, max(0, round(rng.gauss(centre, 1.1))))
            for option, centre in centres.items()
        }
    }


def elect(rng: random.Random) -> Mapping[str, Any] | None:
    popularity = {1: 0.8, 2: 0.45, 3: 0.65, 4: 0.3}
    approved = [option for option, p in popularity.items() if rng.random() < p][:2]
    return {"approved": approved} if approved else None


TRIBUNE = [
    meeting.Member("Marie Dupont", "Présidente de séance"),
    meeting.Member("Paul Martin", "Secrétaire"),
    meeting.Member("Ali Benali", "Scrutateur"),
]
AGENDA = [
    "Accueil et vérification du quorum",
    "Rapport d'activité",
    "Rapport financier 2025",
    "Élection au conseil d'administration",
    "Projets à financer",
    "Lieu de la prochaine assemblée",
    "Questions diverses",
]
FOR_AGAINST: dict[str, Any] = {
    "method": "single_choice",
    "options": ["Pour", "Contre"],
    "params": {"adopt_option": 1},
    "blank_label": "Abstention",
}
ACTIVITY = PollInput(title="Approbation du rapport d'activité", **FOR_AGAINST)
ACCOUNTS = PollInput(title="Approbation des comptes 2025", **FOR_AGAINST)
BOARD = PollInput(
    title="Élection au conseil d'administration (2 sièges)",
    method="approval",
    options=["Alice Martin", "Bruno Petit", "Chloé Durand", "David Leroy"],
    params={"seats": 2, "max_choices": 2},
)
PROJECTS = PollInput(
    title="Projets à financer",
    method="majority_judgment",
    options=["Jardin partagé", "Fresque murale", "Atelier vélo"],
)
VENUE = PollInput(
    title="Lieu de la prochaine assemblée",
    method="single_choice",
    options=["Salle des fêtes", "Maison de quartier", "Médiathèque"],
)


PAST_AGENDA = [
    "Accueil et vérification du quorum",
    "Rapport d'activité",
    "Rapport financier 2024",
    "Élection au conseil d'administration",
    "Projets à financer",
    "Questions diverses",
]
# Each agenda item in turn: the secretary's note, and the poll put to the vote.
PAST_ITEMS: list[tuple[str, Any]] = [
    ("28 personnes comptées à l'entrée ; le quorum est atteint.", None),
    ("Bilan des ateliers du samedi et de la fête de quartier ; pas de question.", None),
    (
        "Comptes présentés par le trésorier ; excédent de 1 240 €.",
        (PollInput(title="Approbation des comptes 2024", **FOR_AGAINST), resolution),
    ),
    ("Deux sièges à pourvoir, quatre candidatures.", (BOARD, elect)),
    ("", (PROJECTS, grades)),
    ("Prochaine assemblée en novembre ; lieu à choisir par le conseil.", None),
]
VOTERS = 31  # simulated voters of the session in progress; the phone makes 32
HEADCOUNT = 33


def certificate(directory: Path) -> tuple[Path, Path]:
    """A self-signed certificate for ``HOST``, valid one day; the browser ignores the issuer."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = directory / "cert.pem", directory / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


@contextmanager
def serve(app: FastAPI, cert_file: Path, key_file: Path) -> Iterator[int]:
    """The application on a free local port, over HTTPS, in a background thread."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        ssl_certfile=str(cert_file),
        ssl_keyfile=str(key_file),
        timeout_graceful_shutdown=1,  # real-time streams stay open
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the server did not start")
        time.sleep(0.05)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(10)


class Scene:
    """The example sessions, written through the services as the console would."""

    def __init__(self, database: Database, keyring: Keyring) -> None:
        self.database = database
        self.keyring = keyring
        self.owner_id = 0
        self.rng = random.Random(2026)  # noqa: S311 (simulated votes, the same at every run)

    def write[T](self, action: Callable[[Session, User], T]) -> T:
        with self.database.write() as db:
            owner = db.get(User, self.owner_id)
            if owner is None:
                raise RuntimeError("prepare() creates the account first")
            return action(db, owner)

    def prepare(self) -> tuple[str, str]:
        """The account, last year's session (closed and voted on), next month's board meeting
        and today's assembly, still in preparation; returns the IDs of the first and the last."""
        now = utcnow()
        with self.database.write() as db:
            owner = accounts.create_user(
                db, EMAIL, PASSWORD, display_name="Les Jardins du Faubourg", verified=True
            )
            self.owner_id = owner.id
            past = self.past_session(db, owner, now)
            board = assemblies.create_assembly(
                db,
                self.keyring,
                owner,
                AssemblyInput(
                    title="Conseil d'administration de décembre",
                    date=now.date() + dt.timedelta(days=35),
                    place="Maison de quartier",
                ),
                now=now - dt.timedelta(days=1),
            ).public_id
            meeting.set_tribune(db, owner, board, TRIBUNE[:1])
            polls.create_poll(db, owner, board, VENUE, now=now)
            today = assemblies.create_assembly(
                db,
                self.keyring,
                owner,
                AssemblyInput(
                    title="Assemblée générale 2026",
                    date=now.date(),
                    place="Salle des fêtes",
                    registered_count=40,
                    quorum_rule="1/4",
                ),
                now=now,
            ).public_id
            meeting.set_tribune(db, owner, today, TRIBUNE)
            meeting.set_agenda(db, owner, today, AGENDA)
            for data in (ACTIVITY, ACCOUNTS, BOARD, PROJECTS, VENUE):
                polls.create_poll(db, owner, today, data, now=now)
        return past, today

    def past_session(self, db: Session, owner: User, now: dt.datetime) -> str:
        """Last year's assembly, from its opening to its closing on a Saturday afternoon."""
        start = dt.datetime(now.year - 1, 11, 8, 13, 0, tzinfo=dt.UTC)  # 14:00 in Paris

        def at(minutes: int) -> dt.datetime:
            return start + dt.timedelta(minutes=minutes)

        public_id = assemblies.create_assembly(
            db,
            self.keyring,
            owner,
            AssemblyInput(
                title=f"Assemblée générale {start.year}",
                date=start.date(),
                place="Maison des associations",
                registered_count=40,
                quorum_rule="1/4",
            ),
            now=at(-30 * 24 * 60),
        ).public_id
        meeting.set_tribune(db, owner, public_id, TRIBUNE)
        meeting.set_agenda(db, owner, public_id, PAST_AGENDA)
        drafts = {
            position: polls.create_poll(db, owner, public_id, vote[0], now=at(-60)).id
            for position, (_note, vote) in enumerate(PAST_ITEMS, start=1)
            if vote
        }
        assemblies.open_assembly(db, owner, public_id, now=at(0))
        password = access.session_password(db, self.keyring, owner, public_id)
        tokens = [access.join(db, self.keyring, public_id, password).token for _ in range(27)]
        access.set_headcount(db, owner, public_id, 28, now=at(6))
        moments = iter(range(10, 300, 4))
        for position, (text, vote) in enumerate(PAST_ITEMS, start=1):
            meeting.set_current_item(db, owner, public_id, position, now=at(next(moments)))
            if text:
                notes.add_note(db, owner, public_id, text, now=at(next(moments)))
            if vote:
                poll_id, make_ballot = drafts[position], vote[1]
                polls.open_poll(db, owner, poll_id, now=at(next(moments)))
                for token in tokens:
                    ballots.cast_vote(db, public_id, token, poll_id, make_ballot(self.rng))
                polls.close_poll(db, owner, poll_id, now=at(next(moments)))
                polls.publish_result(db, owner, poll_id, voters=True, now=at(next(moments)))
        assemblies.close_assembly(db, owner, public_id, now=at(next(moments)))
        return public_id

    def poll_id(self, public_id: str, title: str) -> int:
        return self.write(
            lambda db, owner: next(
                poll.id for poll in polls.list_polls(db, owner, public_id) if poll.title == title
            )
        )


class Shots:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.count = 0

    def take(self, page: Page, name: str) -> None:
        page.wait_for_timeout(400)  # real-time redraws and transitions
        path = self.out / f"{name}.png"
        page.screenshot(path=path, animations="disabled", caret="hide")
        self.saved(path)

    def saved(self, path: Path) -> None:
        if shutil.which("pngquant"):
            command = ["pngquant", "--force", "--skip-if-larger", "--quality=75-95", "--strip"]
            subprocess.run([*command, "--ext", ".png", str(path)], check=False)  # noqa: S603
        self.count += 1
        print(f"  {path}")


def log_in(page: Page) -> None:
    page.goto("/login")
    page.fill("#field-email", EMAIL)
    page.fill("#field-password", PASSWORD)
    page.locator("form[action='/login'] button[type=submit]").click()
    expect(page.get_by_text("Vous êtes connecté·e.")).to_be_visible()


def show_rubric(page: Page, rubric: str) -> None:
    """A rubric of the session console, through the sidebar or the tabs of a phone."""
    href = f"#tab-{rubric}"
    side = page.locator(f".rubric-nav a[href='{href}']")
    if side.is_visible():
        side.click()
    else:
        page.locator(f".tab-bar > a[href='{href}']").click()
    expect(page.locator(href)).to_be_visible()
    page.evaluate("window.scrollTo(0, 0)")


def scroll_to(page: Page, selector: str, margin: int = 16) -> None:
    page.locator(selector).first.evaluate(
        "(el, margin) => window.scrollTo(0, el.getBoundingClientRect().top + scrollY - margin)",
        margin,
    )


def run(browser: Browser, phone: dict[str, Any], scene: Scene, shots: Shots) -> None:
    past, today = scene.prepare()

    def context(**options: Any) -> BrowserContext:
        return browser.new_context(base_url=BASE, ignore_https_errors=True, **options)

    tribune = context(**DESKTOP).new_page()
    log_in(tribune)

    # Before the session: the checklist at the head of Séance (no Conduite yet), and a poll that
    # begins with its voting method.
    tribune.goto(f"/manage/{today}")
    show_rubric(tribune, "meeting")
    shots.take(tribune, "console-checklist")
    tribune.set_viewport_size(NARROW)  # a centred page: no empty margins
    tribune.goto(f"/manage/{today}/polls/new")
    # As tall as the cards: every voting method in view, down to the last row.
    bottom = tribune.locator(".method-cards").evaluate("el => el.getBoundingClientRect().bottom")
    tribune.set_viewport_size({"width": NARROW["width"], "height": int(bottom) + 12})
    shots.take(tribune, "poll-methods")
    tribune.set_viewport_size(DESKTOP["viewport"])

    # The session opens and the room joins.
    def open_session(db: Session, owner: User) -> tuple[str, list[str]]:
        assemblies.open_assembly(db, owner, today)
        meeting.set_current_item(db, owner, today, 2)
        password = access.session_password(db, scene.keyring, owner, today)
        tokens = [access.join(db, scene.keyring, today, password).token for _ in range(VOTERS)]
        return password, tokens

    password, tokens = scene.write(open_session)
    tribune.goto("/dashboard")
    shots.take(tribune, "dashboard")

    # A voter: the join page and the commitments of the service, then the QR code (password in
    # the fragment) in a new page, since a change of fragment alone reloads nothing.
    voters = context(**phone)
    visitor = voters.new_page()
    visitor.goto(f"/{today}")
    shots.take(visitor, "voter-join")
    visitor.locator("details.about > summary").click()
    shots.take(visitor, "voter-commitments")
    visitor.close()
    voter = voters.new_page()
    voter.goto(f"/{today}#k={password}")
    expect(voter.get_by_text("En attente du prochain vote")).to_be_visible()

    # A first poll voted on, closed and published; then Majority Judgment, open.
    activity = scene.poll_id(today, ACTIVITY.title)
    projects = scene.poll_id(today, PROJECTS.title)

    def first_polls(db: Session, owner: User) -> None:
        polls.open_poll(db, owner, activity)
        for token in tokens:
            ballots.cast_vote(db, today, token, activity, resolution(scene.rng))
        polls.close_poll(db, owner, activity)
        polls.publish_result(db, owner, activity, voters=True)
        notes.add_note(
            db, owner, today, "Rapport présenté par Marie Dupont ; deux questions sur les ateliers."
        )
        access.set_headcount(db, owner, today, HEADCOUNT)
        meeting.set_current_item(db, owner, today, 5)
        polls.open_poll(db, owner, projects)
        for token in tokens[:23]:
            ballots.cast_vote(db, today, token, projects, grades(scene.rng))
        timer.start(db, owner, today, duration=180, label="Temps de parole")

    scene.write(first_polls)

    # The phone grades, checks and confirms.
    voter.reload()
    ballot = voter.locator(f"#poll-{projects}")
    expect(ballot).to_be_visible()
    for option, grade in (("Jardin partagé", "Très bien"), ("Fresque murale", "Passable")):
        ballot.locator("fieldset", has_text=option).get_by_text(grade, exact=True).click()
    voter.evaluate("window.scrollTo(0, 0)")
    shots.take(voter, "voter-ballot")
    ballot.locator("fieldset", has_text="Atelier vélo").get_by_text("Bien", exact=True).click()
    ballot.get_by_role("button", name="Continuer").click()
    expect(ballot.locator(".pending-flag")).to_be_visible()
    voter.evaluate("window.scrollTo(0, 0)")
    shots.take(voter, "voter-check")
    ballot.get_by_role("button", name="Confirmer mon vote").click()
    expect(ballot.get_by_role("heading", name="Vote enregistré")).to_be_visible()

    # The tribune's commands and the projection screen while the vote is open.
    tribune.goto(f"/manage/{today}")
    show_rubric(tribune, "cockpit")
    shots.take(tribune, "console-steering")
    token = scene.write(lambda db, owner: access.screen_token(db, scene.keyring, owner, today))
    screen = context(**SCREEN).new_page()
    screen.goto(f"/{today}/screen/{token}")
    shots.take(screen, "screen-vote")

    # Closed, published to the phones and shown on the screen; entries locked.
    def close_projects(db: Session, owner: User) -> None:
        for voter_token in tokens[23:28]:
            ballots.cast_vote(db, today, voter_token, projects, grades(scene.rng))
        polls.close_poll(db, owner, projects)
        polls.publish_result(db, owner, projects, voters=True)
        polls.show_on_screen(db, owner, projects, True)
        access.set_entries_locked(db, owner, today, True)
        timer.reset(db, owner, today)
        timer.show(db, owner, today, False)

    scene.write(close_projects)
    screen.reload()
    shots.take(screen, "screen-result")
    voter.reload()
    scroll_to(voter, "#live h2:text('Résultats publiés')")
    shots.take(voter, "voter-result")

    # Delegate links: Settings on the computer, the secretary's console on a phone.
    def hand_over(db: Session, owner: User) -> str:
        _delegate, key = delegates.create_delegate(
            db, scene.keyring, owner, today, "Paul Martin, secrétaire", ["agenda", "notes", "timer"]
        )
        delegates.create_delegate(
            db, scene.keyring, owner, today, "Ali Benali, scrutateur", ["polls", "headcount"]
        )
        return key

    key = scene.write(hand_over)
    tribune.goto(f"/manage/{today}")
    show_rubric(tribune, "settings")
    shots.take(tribune, "console-delegates")
    secretary = context(**phone).new_page()
    secretary.goto(f"/manage/{today}/delegate#k={key}")
    expect(secretary.locator("#tab-cockpit")).to_be_visible()
    show_rubric(secretary, "agenda")
    shots.take(secretary, "delegate-phone")

    # Last year's minutes, rendered as the console's download would be: the course of the
    # session, then the detail of the polls.
    if not shutil.which("pdftoppm"):
        print("  pdftoppm missing: no image of the minutes")
        return
    record = scene.write(lambda db, owner: exports.session_record(db, owner, past))
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "minutes.pdf"
        pdf.write_bytes(minutes.render(record, now=utcnow()))
        for page, name in MINUTES_PAGES.items():
            command = ["pdftoppm", "-png", "-r", str(MINUTES_DPI), "-f", str(page), "-l", str(page)]
            target = shots.out / name
            subprocess.run([*command, "-singlefile", str(pdf), str(target)], check=True)  # noqa: S603
            shots.saved(target.with_suffix(".png"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="agaura-shots-") as tmp:
        data = Path(tmp)
        settings = Settings(_env_file=None, data_dir=data, demo=False)  # type: ignore[call-arg]
        database = Database(settings.sqlalchemy_url, audit_key=settings.audit_key)
        database.upgrade()
        scene = Scene(database, Keyring(settings.secret_key.get_secret_value()))
        shots = Shots(args.out)
        with serve(create_app(settings), *certificate(data)) as port, sync_playwright() as pw:
            # The name of production, resolved to the local port.
            browser = pw.chromium.launch(
                args=[f"--host-resolver-rules=MAP {HOST} 127.0.0.1:{port}"]
            )
            phone = {**pw.devices[PHONE], **PHONE_SIZE}
            try:
                run(browser, phone, scene, shots)
            finally:
                browser.close()
        database.dispose()
    print(f"{shots.count} screenshots in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
