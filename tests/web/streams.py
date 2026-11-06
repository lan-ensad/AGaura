"""Server-Sent Events in tests: the test client returns a streaming answer only once it ends, so
another thread acts while the stream is open."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi.testclient import TestClient
from httpx import Response

from agaura.web.realtime import Hub

WAIT = 5.0


def wait_for(condition: Callable[[], bool], timeout: float = WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


@dataclass
class Followed:
    response: Response
    ended_by_itself: bool  # False: the hub had to close it


def follow(
    client: TestClient, url: str, hub: Hub, channel: str, meanwhile: Callable[[], None]
) -> Followed:
    """Open the stream at ``url``, run ``meanwhile`` once it follows ``channel``, then wait for
    the stream to end; past ``WAIT`` seconds the hub closes it."""
    ended: list[bool] = []

    def other() -> None:
        assert wait_for(lambda: hub.subscribers(channel) > 0), "the stream never opened"
        meanwhile()
        ended.append(wait_for(lambda: hub.subscribers(channel) == 0))
        if not ended[0]:
            hub.close()

    thread = threading.Thread(target=other)
    thread.start()
    response = client.get(url)
    thread.join()
    return Followed(response, bool(ended and ended[0]))


class Held:
    """Streams open in background threads; their answers arrive once the hub closes."""

    def __init__(self, client: TestClient, url: str, count: int) -> None:
        self.responses: list[Response] = []
        self.threads = [
            threading.Thread(target=lambda: self.responses.append(client.get(url)))
            for _ in range(count)
        ]
        for thread in self.threads:
            thread.start()

    def close(self, hub: Hub) -> list[Response]:
        hub.close()
        for thread in self.threads:
            thread.join()
        return self.responses


def hold_streams(client: TestClient, url: str, hub: Hub, channel: str, count: int) -> Held:
    """Open ``count`` streams at ``url`` and wait until they follow ``channel``."""
    before = hub.subscribers(channel)
    held = Held(client, url, count)
    assert wait_for(lambda: hub.subscribers(channel) == before + count), "streams not open"
    return held
