"""Limits of the real-time hub (SPEC §9.3): streams per holder, per channel and per process,
and the re-check of each holder's access while its stream is open."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack

import pytest

from agaura.web import realtime
from agaura.web.realtime import STREAM_CAPS, Holder, Hub, stream

ALICE = Holder("voter", (1,))
BRUNO = Holder("voter", (2,))


def hold(hub: Hub, stack: ExitStack, channel: str, holder: Holder, count: int) -> list[bool]:
    return [stack.enter_context(hub.hold(channel, holder)) for _ in range(count)]


def test_streams_per_holder() -> None:
    hub = Hub()
    cap = STREAM_CAPS["voter"]
    with ExitStack() as stack:
        assert hold(hub, stack, "live:A", ALICE, cap + 1) == [True] * cap + [False]
        assert hub.streams(ALICE) == cap and hub.streams() == cap  # the refusal counts nothing
        assert hold(hub, stack, "live:A", BRUNO, 1) == [True]  # another device
        # Other kinds of holders have their own caps.
        screen = Holder("screen", ("A",))
        assert hold(hub, stack, "manage:A", screen, 7) == [True] * 6 + [False]
        console = Holder("console", ("A", None))
        assert hold(hub, stack, "manage:A", console, 9) == [True] * 8 + [False]
    assert hub.streams() == 0  # slots given back when the streams end
    with hub.hold("live:A", ALICE) as held:
        assert held


def test_ceilings_per_channel_and_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(realtime, "MAX_STREAMS_PER_CHANNEL", 3)
    monkeypatch.setattr(realtime, "MAX_STREAMS", 5)
    hub = Hub()
    voters = [Holder("voter", (number,)) for number in range(10)]
    with ExitStack() as stack:
        granted = [stack.enter_context(hub.hold("live:A", voter)) for voter in voters[:4]]
        assert granted == [True, True, True, False]  # one session cannot take everything
        granted = [stack.enter_context(hub.hold("live:B", voter)) for voter in voters[4:7]]
        assert granted == [True, True, False]  # the process is full
        assert hub.streams() == 5
    assert hub.streams() == 0


def test_a_slot_is_given_back_when_the_stream_fails() -> None:
    hub = Hub()
    with pytest.raises(RuntimeError), hub.hold("live:A", ALICE):
        raise RuntimeError
    assert hub.streams() == 0


def test_stream_stops_before_an_event_once_refused() -> None:
    async def scenario() -> list[str]:
        hub = Hub()
        hub.bind(asyncio.get_running_loop())
        allowed = [True]
        received: list[str] = []

        async def listen() -> None:
            async for message in stream(hub, "live:A", lambda: allowed[0]):
                received.append(message.data)

        task = asyncio.create_task(listen())
        await asyncio.sleep(0.01)
        hub.publish("live:A", "update", "before")
        await asyncio.sleep(0.05)
        allowed[0] = False  # e.g. the credential was revoked
        hub.publish("live:A", "update", "after")
        await asyncio.wait_for(task, 1)
        assert hub.subscribers("live:A") == 0
        return received

    assert asyncio.run(scenario()) == ["before"]


def test_periodic_check_without_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """At least every RECHECK_INTERVAL seconds, spread at random."""
    monkeypatch.setattr(realtime, "RECHECK_INTERVAL", 0.05)
    checks: list[bool] = []

    def allowed() -> bool:
        checks.append(True)
        return len(checks) < 3

    async def scenario() -> list[str]:
        hub = Hub()
        received = [message.data async for message in stream(hub, "live:A", allowed)]
        return received

    assert asyncio.run(asyncio.wait_for(scenario(), 2)) == []
    assert len(checks) == 3


def test_close_from_another_thread_wakes_streams_at_once() -> None:
    async def scenario() -> float:
        hub = Hub()
        hub.bind(asyncio.get_running_loop())
        task = asyncio.create_task(anext(aiter(stream(hub, "live:A")), None))
        await asyncio.sleep(0.01)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.to_thread(hub.close)
        assert await asyncio.wait_for(task, 1) is None
        return loop.time() - started

    assert asyncio.run(scenario()) < 0.5
