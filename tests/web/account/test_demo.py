"""Demo pages (FR-09a): off by default; ``/demo`` and its anti-robot check, the banner, the
account page of a demo, ending it, the caps."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from agaura import tasks
from agaura.config import Settings
from agaura.main import create_app
from agaura.models import Assembly, AssemblyStatus, Poll, User, UserSession, utcnow
from agaura.services import access, assemblies, delegates
from agaura.web.account.demo import DEMOS_PER_IP
from agaura.web.security import SESSION_COOKIE
from tests.conftest import World
from tests.web.account.conftest import altcha
from tests.web.conftest import log_in, make_account, post
from tests.web.live.conftest import join
from tests.web.manage.test_delegates import open_link
from tests.web.screen.conftest import screen_url

BANNER = "Rien n'est conservé\u00a0: ce compte et ses sessions sont effacés"
VOTER_BANNER = "Démonstration</strong> · 5 votants au plus\u00a0; rien n'est conservé"
DELEGATE_BANNER = (
    "Démonstration</strong> · 5 votants au plus, aucun export\u00a0; rien n'est conservé"
)
CONTACT = '<a href="mailto:contact@agaura.org">contact@agaura.org</a>'
EXPORTS = ("minutes.pdf", "export.json")


def demo_app(settings: Settings, database_path: Path, **changes: object) -> FastAPI:
    update = {"database_url": f"sqlite:///{database_path}", "demo": True, **changes}
    return create_app(settings.model_copy(update=update))


@pytest.fixture
def demo_client(settings: Settings, database_path: Path) -> Iterator[TestClient]:
    with TestClient(demo_app(settings, database_path), follow_redirects=False) as client:
        yield client


def open_demo(client: TestClient) -> None:
    response = post(client, "/demo", {"altcha": altcha(client)})
    assert response.status_code == 303 and response.headers["location"] == "/dashboard"


def demo_user(client: TestClient) -> User:
    with client.app.state.database.read() as db:  # type: ignore[attr-defined]
        return db.scalars(select(User).where(User.is_demo.is_(True))).one()


def users(client: TestClient) -> int:
    with client.app.state.database.read() as db:  # type: ignore[attr-defined]
        return db.scalar(select(func.count()).select_from(User)) or 0


def test_on_by_default() -> None:
    assert Settings(_env_file=None).demo is True  # type: ignore[call-arg]


def test_turned_off(settings: Settings, database_path: Path) -> None:
    with TestClient(
        demo_app(settings, database_path, demo=False), follow_redirects=False
    ) as client:
        assert client.get("/demo").status_code == 404
        assert post(client, "/demo").status_code == 404
        for path in ("/", "/login"):
            page = client.get(path).text
            assert 'href="/demo"' not in page and "Essayer sans compte" not in page, path


def test_home_and_login_offer_the_demo(demo_client: TestClient) -> None:
    for path, heading in (("/", "h3"), ("/login", "h2")):
        page = demo_client.get(path).text
        assert f'<{heading} class="demo-offer-title">' in page, path
        assert "Essayer sans compte" in page, path
        assert "Ses limites\u00a0: 5 votants par session, aucun export" in page, path
        assert 'href="/demo">Essayer la démo</a>' in page, path


def test_the_demo_page(demo_client: TestClient) -> None:
    page = demo_client.get("/demo")
    assert page.status_code == 200
    assert "Ouvrir la démo" in page.text and 'name="altcha"' in page.text
    for limit in (
        "Chaque session accepte 5 votants au plus",
        "Aucun export\u00a0: le procès-verbal et les fichiers CSV et JSON demandent un compte. "
        f"Pour en obtenir un, écrivez à {CONTACT}.",
        "3 sessions non terminées à la fois au plus, 20 votes par session.",
        "effacés 2 heures après votre dernière action, au plus tard 24 heures après l'ouverture",
    ):
        assert limit in page.text, limit
    assert "/static/js/pow.js" in page.text


def test_the_anti_robot_check_is_required(demo_client: TestClient) -> None:
    response = post(demo_client, "/demo", {"altcha": ""})
    assert response.status_code == 400
    assert users(demo_client) == 0


def test_opening_a_demo(demo_client: TestClient) -> None:
    open_demo(demo_client)
    assert SESSION_COOKIE in demo_client.cookies
    dashboard = demo_client.get("/dashboard")
    assert "Démo ouverte\u00a0: deux sessions d&#39;exemple sont prêtes." in dashboard.text
    assert BANNER in dashboard.text
    limits = (
        "Limites\u00a0: 5 votants par session, aucun export. "
        f"Pour obtenir un compte\u00a0: {CONTACT}."
    )
    assert limits in dashboard.text
    assert "Assemblée générale de démonstration" in dashboard.text
    assert "Terminer la démo" in dashboard.text and "Se déconnecter" not in dashboard.text
    assert demo_client.get("/demo").headers["location"] == "/dashboard"


def test_opening_a_demo_logs_out_the_previous_account(
    demo_client: TestClient, world: World
) -> None:
    owner = make_account(world)
    log_in(demo_client, owner.email)
    assert "Ouvrir la démo vous déconnecte" in demo_client.get("/demo").text
    open_demo(demo_client)
    assert "owner@example.org" not in demo_client.get("/account").text
    with world.database.read() as db:
        assert db.get(User, owner.id) is not None  # only logged out


def test_account_page_of_a_demo(demo_client: TestClient) -> None:
    open_demo(demo_client)
    page = demo_client.get("/account").text
    assert "Compte de démonstration" in page
    assert "Limites de la démo" in page and "Chaque session accepte 5 votants au plus" in page
    assert "Terminer la démo et tout effacer" in page
    for form in (
        'action="/account/password"',
        'action="/account/email"',
        'action="/account/delete"',
    ):
        assert form not in page
    refused = post(
        demo_client,
        "/account/password",
        {
            "current_password": "",
            "new_password": "a long new password",
            "new_password_confirm": "a long new password",
        },
    )
    assert refused.status_code == 400
    assert demo_user(demo_client).password_hash is None


def test_ending_the_demo_deletes_everything(demo_client: TestClient) -> None:
    open_demo(demo_client)
    response = post(demo_client, "/logout")
    assert response.status_code == 303
    assert users(demo_client) == 0
    with demo_client.app.state.database.read() as db:  # type: ignore[attr-defined]
        assert db.scalar(select(func.count()).select_from(Assembly)) == 0
    assert (
        "Démo terminée\u00a0: son compte et ses sessions sont effacés." in demo_client.get("/").text
    )


def test_voter_page_and_screen_say_demonstration(demo_client: TestClient, world: World) -> None:
    open_demo(demo_client)
    owner = demo_user(demo_client)
    with world.database.write() as db:
        current = db.scalars(
            select(Assembly).where(
                Assembly.owner_id == owner.id, Assembly.status == AssemblyStatus.PREPARATION
            )
        ).one()
        assemblies.open_assembly(db, owner, current.public_id)
        public_id = current.public_id
    voter = TestClient(demo_client.app, follow_redirects=False)
    assert VOTER_BANNER in voter.get(f"/{public_id}").text
    # The steps of a ballot without JavaScript (a page of their own) say it too.
    poll_id = world.poll(owner, public_id)
    join(voter, world, owner, public_id)
    review = post(voter, f"/{public_id}/polls/{poll_id}/review", {"choice": "1"})
    assert review.status_code == 200 and VOTER_BANNER in review.text
    screen = voter.get(screen_url(world, owner, public_id)).text
    assert "AGaura · Démonstration</aside>" in screen
    # A real session says nothing of the kind.
    real = make_account(world, "real@example.org")
    other = world.assembly(real)
    assert "demo-banner" not in voter.get(f"/{other}").text


def test_demos_at_once_are_capped(settings: Settings, database_path: Path) -> None:
    app = demo_app(settings, database_path, demo_max=1)
    with TestClient(app, follow_redirects=False) as first:
        open_demo(first)
    with TestClient(app, follow_redirects=False) as second:
        response = post(second, "/demo", {"altcha": altcha(second)})
        assert response.status_code == 503
        assert "Trop de démos sont ouvertes en ce moment." in response.text


def test_a_full_pool_frees_a_demo_left_idle(settings: Settings, database_path: Path) -> None:
    app = demo_app(settings, database_path, demo_max=1)
    with TestClient(app, follow_redirects=False) as first:
        open_demo(first)
        left = demo_user(first)
        with app.state.database.write() as db:  # its last request, 31 minutes ago
            db.execute(
                update(UserSession)
                .where(UserSession.user_id == left.id)
                .values(expires_at=utcnow() + timedelta(hours=1, minutes=14))
            )
    with TestClient(app, follow_redirects=False) as second:
        open_demo(second)
        assert demo_user(second).email != left.email  # ids are reused: the address is not
        assert users(second) == 1
    with TestClient(app) as visitor:  # the limit is written where it applies
        assert "sans action est effacée pour faire place" in visitor.get("/demo").text


def test_demos_per_address_are_limited(
    demo_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each demo opened by this client ends the login of the one before, and the purge (at the
    # start, then every minute) deletes those: under load, it could run within the loop.
    monkeypatch.setattr(tasks, "purge_demos", lambda *_args: 0)
    for _ in range(DEMOS_PER_IP[0]):
        open_demo(demo_client)
    response = post(demo_client, "/demo", {"altcha": altcha(demo_client)})
    assert response.status_code == 429
    assert users(demo_client) == DEMOS_PER_IP[0]


def test_a_delegate_console_of_a_demo_says_so(demo_client: TestClient, world: World) -> None:
    open_demo(demo_client)
    owner = demo_user(demo_client)
    with world.database.write() as db:
        current = db.scalars(
            select(Assembly).where(
                Assembly.owner_id == owner.id, Assembly.status == AssemblyStatus.PREPARATION
            )
        ).one()
        public_id = current.public_id
        _delegate, key = delegates.create_delegate(
            db, world.keyring, owner, public_id, "Lina", ["notes"]
        )
    browser = open_link(demo_client.app, public_id, key)  # type: ignore[arg-type]
    page = browser.get(f"/manage/{public_id}").text
    assert DELEGATE_BANNER in page


def demo_sessions(client: TestClient) -> tuple[User, Assembly, Assembly]:
    """The demo account, its closed example session and the one in preparation."""
    owner = demo_user(client)
    with client.app.state.database.read() as db:  # type: ignore[attr-defined]
        past, current = db.scalars(
            select(Assembly).where(Assembly.owner_id == owner.id).order_by(Assembly.id)
        ).all()
    return owner, past, current


def test_exports_are_greyed_out_and_refused(demo_client: TestClient, world: World) -> None:
    open_demo(demo_client)
    _owner, past, _current = demo_sessions(demo_client)
    base = f"/manage/{past.public_id}"
    page = demo_client.get(base).text
    for name in EXPORTS:
        assert f'href="{base}/{name}"' not in page
    assert "/ballots.csv" not in page and "/export.json" not in page
    assert 'disabled aria-describedby="exports-demo"' in page
    assert 'disabled aria-describedby="poll-exports-demo"' in page
    assert 'disabled aria-describedby="summary-demo"' in page
    assert (
        "Démo\u00a0: aucun export, ni le procès-verbal ni les fichiers CSV et JSON. Les exports "
        f"demandent un compte\u00a0: pour en obtenir un, écrivez à {CONTACT}."
    ) in page
    with world.database.read() as db:
        poll_id = db.scalars(select(Poll.id).where(Poll.assembly_id == past.id)).first()
    for path in (*EXPORTS, f"polls/{poll_id}/ballots.csv", f"polls/{poll_id}/export.json"):
        response = demo_client.get(f"{base}/{path}")
        assert response.status_code == 403, path
        assert "Les exports demandent un compte" in response.text
        assert "contact@agaura.org" in response.text


def test_the_sixth_voter_is_told_why(demo_client: TestClient, world: World) -> None:
    open_demo(demo_client)
    owner, _past, current = demo_sessions(demo_client)
    with world.database.write() as db:
        assemblies.open_assembly(db, owner, current.public_id)
    world.voters(owner, current.public_id, access.DEMO_VOTERS)
    password = world.password(owner, current.public_id)
    phone = TestClient(demo_client.app, follow_redirects=False)
    phone.get(f"/{current.public_id}")
    refused = post(phone, f"/{current.public_id}/join", {"secret": password})
    assert refused.status_code == 409
    assert "Cette session de démonstration accepte 5 votants au plus" in refused.text
    console = demo_client.get(f"/manage/{current.public_id}").text
    assert "Maximum\u00a0: 5 votants, la limite d'une session de démonstration" in console
    assert "les 5 votants qu'accepte une session de démonstration l'ont rejointe" in console
