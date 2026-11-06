"""In-memory real-time fan-out (SPEC §12.2): one process, one hub, Server-Sent Events.

Events carry no data that matters: they only tell pages *that* something changed, and the pages
fetch the new state through normal, access-checked requests. Channels:

- ``live:{PUBLIC_ID}``: voter pages. Event ``update`` (data: ``polls``, ``results`` or
  ``session``) after a poll is pushed, closed, cancelled, published or dismissed, or the session
  changes state or access mode;
- ``manage:{PUBLIC_ID}``: the session console and the projection screen. Event ``turnout``
  with data ``""`` after a join or a ballot, ``console`` after any console action (including
  deletion of the session), ``polls`` when a timer closed a poll.

``publish`` is thread-safe: synchronous routes run in worker threads and call it after their
transaction has committed.

Each open stream holds a socket, hence a file descriptor, of the single process. A holder (a
voter's device credential, a screen link, the owner or one delegate link at a console) may keep
a few streams at once (``STREAM_CAPS``), and the process as a whole a bounded number
(``MAX_STREAMS_PER_CHANNEL``, ``MAX_STREAMS``); past them a new stream is refused with 429 and the
page falls back to polling. A stream asks again whether its holder may still follow the channel
before passing on each event and at least every ``RECHECK_INTERVAL`` seconds, and ends once the
answer is no (credential revoked or left, link revoked or regenerated, session closed or
deleted).
"""

from __future__ import annotations

import asyncio
import random
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass

from starlette.concurrency import run_in_threadpool

QUEUE_SIZE = 32

# Streams one holder may keep open at once. Refusing the next one (429) rather than closing the
# oldest keeps the first tabs live: browsers reconnect a closed stream by themselves, so tabs
# would take the slot from one another endlessly. A refused page polls every 5 s and tries again.
# A dead connection (a phone that changed network) holds its slot until TCP gives up on it, so
# each cap leaves room for one or two of those.
STREAM_CAPS = {
    "voter": 4,  # one device: two or three tabs of the session, plus a dead connection
    "screen": 6,  # one link: the projector, a second room, a preview at the tribune
    "console": 8,  # the owner's account, or one delegate link: laptop, phone, tablet, tabs
}
# NFR-01 plans 1,000 voters on one instance: one session holding them all with two tabs each
# fits in a channel, and the process keeps room for other sessions, consoles and screens. A
# stream costs a socket (compose.yaml raises the descriptor limit above this) and about 75 KiB
# (measured with uvicorn): 3,000 streams and the largest PDF (550 MiB) stay within the
# container's 1 GB.
MAX_STREAMS_PER_CHANNEL = 2_000
MAX_STREAMS = 3_000
# Bounds how long a holder that lost access keeps its socket when no event comes; any event is
# checked before it is passed on. 30 s costs one small read per stream and per interval (about
# 130 reads per second at ``MAX_STREAMS``), spread at random so streams opened together (after a
# restart) do not check together.
RECHECK_INTERVAL = 30.0


@dataclass(frozen=True, slots=True)
class Message:
    event: str
    data: str = ""


CLOSE = Message("close")


def live_channel(public_id: str) -> str:
    return f"live:{public_id}"


def manage_channel(public_id: str) -> str:
    return f"manage:{public_id}"


def announce_deleted(hub: Hub, public_ids: Iterable[str]) -> None:
    """Voter pages, screens and delegate consoles of deleted sessions check their access at
    once and turn into the page of an unknown session (demo accounts, FR-09a)."""
    for public_id in public_ids:
        hub.publish(live_channel(public_id), "update", "polls")
        hub.publish(manage_channel(public_id), "turnout", "console")


@dataclass(frozen=True, slots=True)
class Holder:
    """Who follows a stream: ``kind`` is a key of ``STREAM_CAPS``, ``key`` tells holders of one
    kind apart (a credential id, a session ID…). Kept in memory only, never logged."""

    kind: str
    key: tuple[object, ...]


class Hub:
    def __init__(self) -> None:
        self._channels: defaultdict[str, set[asyncio.Queue[Message]]] = defaultdict(set)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._held: Counter[Holder] = Counter()
        self._held_on: Counter[str] = Counter()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the event loop that owns the queues (called at application startup)."""
        self._loop = loop

    def subscribers(self, channel: str) -> int:
        return len(self._channels.get(channel, ()))

    def streams(self, holder: Holder | None = None) -> int:
        """Streams held by ``holder``, or by everyone."""
        return self._held[holder] if holder is not None else self._held_on.total()

    @contextmanager
    def hold(self, channel: str, holder: Holder) -> Iterator[bool]:
        """Count one stream of ``holder`` on ``channel`` while the block runs.

        Gives False, and counts nothing, once the holder, the channel or the process has as
        many streams as allowed. Call it from the event loop (an ``async`` dependency): the
        counters have no lock.
        """
        if (
            self._held[holder] >= STREAM_CAPS[holder.kind]
            or self._held_on[channel] >= MAX_STREAMS_PER_CHANNEL
            or self._held_on.total() >= MAX_STREAMS
        ):
            yield False
            return
        self._held[holder] += 1
        self._held_on[channel] += 1
        try:
            yield True
        finally:
            self._held[holder] -= 1
            self._held_on[channel] -= 1
            if not self._held[holder]:
                del self._held[holder]
            if not self._held_on[channel]:
                del self._held_on[channel]

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[asyncio.Queue[Message]]:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Message] = asyncio.Queue(QUEUE_SIZE)
        self._channels[channel].add(queue)
        try:
            yield queue
        finally:
            self._channels[channel].discard(queue)
            if not self._channels[channel]:
                del self._channels[channel]

    def _deliver(self, channel: str, message: Message) -> None:
        for queue in list(self._channels.get(channel, ())):
            if queue.full():
                # A slow client: drop what it has not read, keep the latest news.
                while not queue.empty():
                    queue.get_nowait()
            queue.put_nowait(message)

    def publish(self, channel: str, event: str, data: str = "") -> None:
        """Send an event to every subscriber of ``channel``; callable from any thread."""
        if self._loop is None or self._loop.is_closed():
            return
        message = Message(event, data)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._deliver(channel, message)
        else:
            self._loop.call_soon_threadsafe(self._deliver, channel, message)

    def close(self) -> None:
        """Ask every open stream to end (application shutdown); callable from any thread."""
        if self._loop is None or self._loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._close_all()
        else:
            self._loop.call_soon_threadsafe(self._close_all)

    def _close_all(self) -> None:
        for channel in list(self._channels):
            self._deliver(channel, CLOSE)


def _recheck_delay() -> float:
    return RECHECK_INTERVAL * random.uniform(0.5, 1.0)  # noqa: S311 (spreads load, not a secret)


async def stream(
    hub: Hub, channel: str, allowed: Callable[[], bool] | None = None
) -> AsyncIterator[Message]:
    """Messages of ``channel`` until the hub closes; the caller turns them into SSE events.

    ``allowed`` (blocking: it runs in a worker thread) tells whether the holder may still
    follow the channel. It is asked before each message is passed on and at least every
    ``RECHECK_INTERVAL`` seconds; the stream ends at the first no, so a holder never receives an
    event published after it lost access.
    """
    async with hub.subscribe(channel) as queue:
        while True:
            message: Message | None = None
            try:
                async with asyncio.timeout(_recheck_delay() if allowed is not None else None):
                    message = await queue.get()
            except TimeoutError:
                pass
            if message is CLOSE:
                return
            if allowed is not None and not await run_in_threadpool(allowed):
                return
            if message is not None:
                yield message
