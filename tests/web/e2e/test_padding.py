"""Ballot steps of one size per poll, in a browser (SPEC §9.2): the requests live.js sends and
the answers it gets have the same length whatever the voter chose."""

from __future__ import annotations

import pytest
from playwright.sync_api import BrowserContext, Request, Response, expect

from agaura.models import User
from tests.conftest import World
from tests.web.e2e.conftest import ErrorWatch
from tests.web.live.conftest import APPROVAL, ballots_of

pytestmark = pytest.mark.e2e


def test_ballot_steps_have_one_size(
    phone: BrowserContext, errors: ErrorWatch, world: World, owner: User
) -> None:
    public_id = world.assembly(owner)
    poll_id = world.poll(owner, public_id, APPROVAL)
    voter = errors.watch(phone.new_page())
    requests: list[Request] = []
    responses: list[Response] = []

    def ballot_step(message: Request | Response) -> bool:
        request = message if isinstance(message, Request) else message.request
        return request.method == "POST" and f"/polls/{poll_id}" in request.url

    voter.on("request", lambda request: ballot_step(request) and requests.append(request))
    voter.on("response", lambda response: ballot_step(response) and responses.append(response))
    voter.goto(f"/{public_id}#k={world.password(owner, public_id)}")
    section = voter.locator(f"#poll-{poll_id}")
    size = int(section.locator("form[data-pad]").get_attribute("data-pad") or 0)

    section.get_by_text("Alice", exact=True).click()
    section.get_by_role("button", name="Continuer").click()  # approved=1
    section.get_by_role("button", name="Modifier mon vote").click()  # approved=1, action=edit
    section.get_by_text("Bruno", exact=True).click()
    section.get_by_role("button", name="Continuer").click()  # approved=1, approved=2
    section.get_by_role("button", name="Modifier mon vote").click()
    # At the maximum the third box refuses a tap; only a form changed by hand sends three.
    third = section.get_by_label("Chloé")
    expect(third).to_have_attribute("aria-disabled", "true")
    section.get_by_text("Chloé", exact=True).click(force=True)  # Playwright waits otherwise
    expect(third).not_to_be_checked()
    third.evaluate("box => { box.checked = true; }")
    section.get_by_role("button", name="Continuer").click()  # three: refused, sent back
    expect(section.get_by_role("alert")).to_be_visible()
    section.get_by_text("Vote blanc", exact=True).click()  # unchecks the three boxes
    section.get_by_role("button", name="Continuer").click()  # blank=1
    section.get_by_role("button", name="Confirmer mon vote").click()  # blank=1
    expect(section).to_contain_text("Vote enregistré")

    assert len(requests) == len(responses) == 7
    assert {len(request.post_data_buffer or b"") for request in requests} == {size}
    assert all("&pad=" in (request.post_data or "") for request in requests)
    assert len({len(response.body()) for response in responses}) == 1
    assert [response.status for response in responses] == [200] * 4 + [422, 200, 200]
    assert ballots_of(world, poll_id) == [(True, None)]
    assert errors.unexpected("422") == []  # the refused ballot, logged by the browser
