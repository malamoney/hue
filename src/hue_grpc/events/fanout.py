"""One connection to the Bridge, many subscribers, and the Gaps between.

The Bridge serves one event stream and this Gateway opens one, whatever
number of gRPC clients are listening. Everything else here follows from that
single connection being shared:

**A subscriber cannot slow the reader down.** Delivery is a `put` into a
bounded queue that never waits and never fails — a client that stops reading
fills its own queue and nothing else. What it loses it is told about, by a
`Gap` in the position the events would have been.

**Every reconnect announces a Gap, unconditionally.** No attempt is made to
work out whether anything was actually missed, because that cannot be worked
out: the Bridge discards buffered events without saying it has. See
`CONTEXT.md`. Suppressing the announcement when the Gateway happened to be
away only briefly would replace an honest "something may have happened" with
a false "nothing did".

**Every reconnect is followed by a Resync**, which is what makes the Gap
survivable: see `hue_grpc.events.resync`.

The reader task is `run`, and it never stops on its own. A Bridge that is
unreachable, that hangs up, or that sends a frame this Gateway cannot read is
the same event — the stream ended — and the answer to all three is to open it
again on a bounded, jittered schedule.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import enum
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from hue_grpc.events.resync import Snapshot, differences, remember, snapshot
from hue_grpc.hue.events import BridgeEvent
from hue_grpc.hue.retry import full_jitter
from hue_grpc.hue.transport import HueTransportError
from hue_grpc.logs import fields

__all__ = [
    "DEFAULT_BACKOFF",
    "DEFAULT_QUEUE_SIZE",
    "EVERYTHING",
    "Backoff",
    "Cause",
    "Change",
    "EventFanout",
    "EventSource",
    "Filter",
    "Gap",
    "LightReader",
    "Notice",
    "Subscription",
]

#: How many notices a subscriber may fall behind by before it starts losing
#: them. Generous: the Bridge's own bursts are a few dozen events at most, so
#: reaching this means a client that has stopped reading rather than one
#: having a slow moment.
DEFAULT_QUEUE_SIZE = 256

_log = logging.getLogger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


@dataclass(frozen=True)
class Backoff:
    """How long to wait before opening the stream again, and again after that.

    Separate from `hue_grpc.hue.retry`, which is a policy about whether one
    request may be sent twice and is bounded by the caller's deadline. This
    one is unbounded in attempts — a Bridge that is unplugged for a day is
    still the Bridge — and bounded in delay, so a Gateway that has been
    waiting all night notices its Bridge within `ceiling` of it coming back.
    """

    #: The ceiling on the first wait. It doubles for each one after.
    base: float = 0.5
    #: Where the doubling stops.
    ceiling: float = 30.0
    #: How a wait is drawn from its ceiling. `hue_grpc.hue.retry`'s draw, for
    #: the reason it gives: several gateways that lost the same Bridge must
    #: not come back to it in step.
    jitter: Callable[[float], float] = full_jitter

    def __post_init__(self) -> None:
        if self.base < 0 or self.ceiling < self.base:
            raise ValueError(
                f"a backoff runs from base to ceiling: {self.base} to {self.ceiling}"
            )

    def delays(self) -> Iterator[float]:
        """Successive waits, forever. Started again after every connection."""
        ceiling = self.base
        while True:
            yield self.jitter(ceiling)
            ceiling = min(self.ceiling, ceiling * 2)


#: The schedule a gateway reconnects on unless something says otherwise.
DEFAULT_BACKOFF = Backoff()


class Cause(enum.Enum):
    """Why a subscriber is being told it may have missed something."""

    #: The Gateway's own connection to the Bridge dropped and was remade.
    RECONNECTED = "reconnected"
    #: This subscriber's queue filled while it was not reading.
    SUBSCRIBER_BEHIND = "subscriber_behind"


@dataclass(frozen=True)
class Change:
    """A Resource changed, and when the Gateway learned of it."""

    received: dt.datetime
    event: BridgeEvent


@dataclass(frozen=True)
class Gap:
    """Events may have been missed. See `CONTEXT.md`; it cannot be disproven."""

    received: dt.datetime
    cause: Cause
    #: How many were dropped, where that is knowable — which it is only for
    #: `SUBSCRIBER_BEHIND`, where the Gateway held them and let them go.
    missed: int = 0


#: What a subscriber is handed. Both halves matter: a stream of changes with
#: no way to say "and I may have missed some" would be quietly wrong.
Notice = Change | Gap


@dataclass(frozen=True)
class Filter:
    """What one subscriber asked to see. Empty means everything."""

    resource_ids: frozenset[str] = field(default_factory=frozenset)
    resource_types: frozenset[str] = field(default_factory=frozenset)

    def wants(self, notice: Notice) -> bool:
        """A Gap always passes: it is about the stream, not about a Resource.

        A subscriber narrowed to one light still has to be told the Gateway
        stopped watching it, or the filter turns into a false all-clear.
        """
        if isinstance(notice, Gap):
            return True
        if self.resource_ids and notice.event.resource_id not in self.resource_ids:
            return False
        return not (
            self.resource_types
            and notice.event.resource_type not in self.resource_types
        )


#: The filter that lets everything through, which is what a subscriber that
#: named nothing asked for.
EVERYTHING = Filter()


class Subscription:
    """One subscriber's queue, and the async iterator that drains it.

    Bounded, and it drops rather than waits: `offer` is called from the task
    reading the Bridge, which must never be held up by a client. What was
    dropped is not lost quietly — the next thing the subscriber reads after a
    drop is a `Gap` saying how many, in the position they would have been.
    """

    def __init__(self, *, capacity: int, wanted: Filter) -> None:
        if capacity < 1:
            raise ValueError(f"a subscriber needs room for one notice: {capacity}")
        self._capacity = capacity
        self._wanted = wanted
        self._queue: deque[Notice] = deque()
        self._ready = asyncio.Event()
        self._missed = 0
        self._missed_since: dt.datetime | None = None
        self._closed = False

    def offer(self, notice: Notice) -> None:
        """Hand one notice over, or count it as missed. Never waits."""
        if not self._wanted.wants(notice):
            return
        if self._missed and len(self._queue) + 1 < self._capacity:
            self._queue.append(self._gap())
        if len(self._queue) >= self._capacity:
            self._missed += 1
            self._missed_since = self._missed_since or _now()
            return
        self._queue.append(notice)
        self._ready.set()

    def close(self) -> None:
        """End the stream for whoever is reading it, once the queue is dry."""
        self._closed = True
        self._ready.set()

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Notice:
        while True:
            if self._queue:
                return self._queue.popleft()
            if self._missed:
                # Nothing left to hand over and something to own up to. A
                # subscriber that catches up during a quiet spell is told
                # then, rather than whenever the Bridge next says something.
                return self._gap()
            if self._closed:
                raise StopAsyncIteration
            self._ready.clear()
            if self._queue or self._missed or self._closed:
                continue
            await self._ready.wait()

    def _gap(self) -> Gap:
        gap = Gap(
            received=self._missed_since or _now(),
            cause=Cause.SUBSCRIBER_BEHIND,
            missed=self._missed,
        )
        self._missed = 0
        self._missed_since = None
        return gap


class EventSource(Protocol):
    """The Bridge's event stream, as this module needs it.

    `hue_grpc.hue.events.BridgeEvents` is the one implementation; the shape is
    stated here so that what the fanout does when a connection ends can be
    tested by ending one.
    """

    def connected(
        self,
    ) -> AbstractAsyncContextManager[AsyncIterator[list[BridgeEvent]]]: ...


class LightReader(Protocol):
    """The full read a Resync is made of. `hue_grpc.hue.lights.Lights`."""

    async def all(self) -> list[Mapping[str, Any]]: ...


class EventFanout:
    """The Bridge's event stream, shared out among gRPC subscribers."""

    def __init__(
        self,
        *,
        events: EventSource,
        lights: LightReader,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        backoff: Backoff = DEFAULT_BACKOFF,
    ) -> None:
        if queue_size < 1:
            raise ValueError(f"a subscriber needs room for one notice: {queue_size}")
        self._events = events
        self._lights = lights
        self._queue_size = queue_size
        self._backoff = backoff
        self._subscriptions: set[Subscription] = set()
        #: What the last full read said, or `None` before one has succeeded.
        #: A Resync with nothing to compare against seeds instead of reporting
        #: every light as news.
        self._known: Snapshot | None = None

    @property
    def subscribers(self) -> int:
        return len(self._subscriptions)

    @asynccontextmanager
    async def subscribe(
        self, wanted: Filter = EVERYTHING
    ) -> AsyncIterator[Subscription]:
        """A queue of this subscriber's own, for as long as the block runs."""
        subscription = Subscription(capacity=self._queue_size, wanted=wanted)
        self._subscriptions.add(subscription)
        try:
            yield subscription
        finally:
            self._subscriptions.discard(subscription)
            subscription.close()

    async def run(self) -> None:
        """Read the Bridge until cancelled, opening the stream as often as needed."""
        delays = self._backoff.delays()
        opened = False
        try:
            while True:
                try:
                    async with self._events.connected() as batches:
                        await self._opened(reconnected=opened)
                        opened = True
                        delays = self._backoff.delays()
                        async for batch in batches:
                            self._deliver(batch)
                    _log.info("the bridge closed the event stream")
                except HueTransportError as lost:
                    # Unreachable, hung up, or sending frames that cannot be
                    # read: all of them are the stream having ended, and the
                    # answer to all of them is to open it again.
                    _log.warning(
                        "event stream lost",
                        **fields(failure=type(lost).__name__, detail=str(lost)),
                    )
                await asyncio.sleep(next(delays))
        finally:
            # Whatever stopped the reader — cancellation on shutdown, or a
            # failure that is not the Bridge's — a subscriber left waiting on
            # a stream nobody is filling would wait forever.
            for subscription in list(self._subscriptions):
                subscription.close()

    async def _opened(self, *, reconnected: bool) -> None:
        """Say what a fresh connection means, before reading anything from it."""
        if reconnected:
            self._publish(Gap(received=_now(), cause=Cause.RECONNECTED))
        await self._resync(announce=reconnected)

    async def _resync(self, *, announce: bool) -> None:
        """Re-read the lights, and say how they differ from what we believed.

        A read that fails is logged and left: the Gap has already been
        announced, and taking the stream back down because the collection
        could not be read would cost the events that are about to arrive on it.
        """
        try:
            lights = await self._lights.all()
        except HueTransportError as unread:
            _log.warning(
                "could not resync after opening the event stream",
                **fields(failure=type(unread).__name__, detail=str(unread)),
            )
            return
        fresh = snapshot(lights)
        believed, self._known = self._known, fresh
        if not announce or believed is None:
            return
        received = _now()
        changes = differences(believed, fresh)
        if changes:
            _log.info("resynced after a gap", **fields(changes=len(changes)))
        for event in changes:
            self._publish(Change(received=received, event=event))

    def _deliver(self, batch: Sequence[BridgeEvent]) -> None:
        received = _now()
        for event in batch:
            if self._known is not None:
                remember(self._known, event)
            self._publish(Change(received=received, event=event))

    def _publish(self, notice: Notice) -> None:
        for subscription in list(self._subscriptions):
            subscription.offer(notice)
