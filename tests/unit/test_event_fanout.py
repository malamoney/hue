"""One upstream stream, many subscribers, and the Gaps between them.

The Bridge is scripted here rather than served over a socket: what these
tests are about is what happens when the connection ends, which is a thing to
arrange rather than to wait for. `test_bridge_events` covers the bytes and
`test_event_service` covers the whole way out to a gRPC client.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from conftest import run

from hue_grpc.events.fanout import (
    Backoff,
    Cause,
    Change,
    EventFanout,
    Filter,
    Gap,
    Notice,
    Subscription,
)
from hue_grpc.hue.events import BridgeEvent
from hue_grpc.hue.transport import BridgeUnreachableError, MalformedResponseError

A = "8f2a1e00-0000-4000-8000-00000000000a"
B = "8f2a1e00-0000-4000-8000-00000000000b"

#: No waiting: what the reconnect schedule is worth is its own test.
IMMEDIATELY = Backoff(base=0.0, ceiling=0.0)


def change(
    resource_id: str, *, kind: str = "update", type_: str = "light", **properties: Any
) -> BridgeEvent:
    return BridgeEvent(
        id=f"event-for-{resource_id}",
        type=kind,
        created=None,
        resource={"id": resource_id, "type": type_, **properties},
    )


def light(resource_id: str, **properties: Any) -> dict[str, Any]:
    return {"id": resource_id, "type": "light", **properties}


@dataclass
class Attempt:
    """One connection to the Bridge, and what it does before it ends."""

    batches: Sequence[Sequence[BridgeEvent]] = ()
    #: Raised instead of connecting, which is a Bridge that is not there.
    fails: Exception | None = None
    #: Stay open after the batches, as a healthy Bridge does between events.
    holds: bool = False


class ScriptedEvents:
    """An upstream stream that serves each connection from a script.

    The last attempt is served again for every connection after it, so a
    script ending in one that holds settles rather than spinning.
    """

    def __init__(self, *attempts: Attempt) -> None:
        self._attempts = attempts or (Attempt(holds=True),)
        self.connections = 0
        #: Set once a connection has handed over every batch it was given,
        #: which is how a test waits for the reader rather than for a client.
        self.drained = asyncio.Event()

    @asynccontextmanager
    async def connected(self) -> AsyncIterator[AsyncIterator[list[BridgeEvent]]]:
        attempt = self._attempts[min(self.connections, len(self._attempts) - 1)]
        self.connections += 1
        if attempt.fails is not None:
            raise attempt.fails
        yield self._batches(attempt)

    async def _batches(self, attempt: Attempt) -> AsyncIterator[list[BridgeEvent]]:
        for batch in attempt.batches:
            yield list(batch)
        self.drained.set()
        if attempt.holds:
            await asyncio.sleep(3600)


class ScriptedLights:
    """The light collection, as it reads on each successive Resync."""

    def __init__(self, *reads: Sequence[Mapping[str, Any]] | Exception) -> None:
        self._reads = reads or ((),)
        self.reads = 0

    async def all(self) -> list[Mapping[str, Any]]:
        read = self._reads[min(self.reads, len(self._reads) - 1)]
        self.reads += 1
        if isinstance(read, Exception):
            raise read
        return list(read)


def fanout(
    events: ScriptedEvents | None = None,
    lights: ScriptedLights | None = None,
    *,
    queue_size: int = 64,
) -> EventFanout:
    return EventFanout(
        events=events or ScriptedEvents(),
        lights=lights or ScriptedLights(),
        queue_size=queue_size,
        backoff=IMMEDIATELY,
    )


@asynccontextmanager
async def running(fanout: EventFanout) -> AsyncIterator[None]:
    """The fanout's upstream reader, for as long as the block runs."""
    reader = asyncio.create_task(fanout.run())
    try:
        yield
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def take(subscription: Subscription, count: int) -> list[Notice]:
    """The next `count` notices, or a failed test if they do not arrive."""
    return [await asyncio.wait_for(anext(subscription), 2) for _ in range(count)]


async def nothing_more(subscription: Subscription) -> bool:
    try:
        await asyncio.wait_for(anext(subscription), 0.1)
    except TimeoutError:
        return True
    return False


def changed(notices: Sequence[Notice]) -> list[tuple[str, str]]:
    """The resource id and event type of each change, gaps left out."""
    return [
        (notice.event.resource_id, notice.event.type)
        for notice in notices
        if isinstance(notice, Change)
    ]


# Fanning out


def test_delivers_every_change_to_every_subscriber() -> None:
    events = ScriptedEvents(Attempt(batches=[[change(A), change(B)]], holds=True))
    fan = fanout(events)

    async def scenario() -> None:
        async with running(fan), fan.subscribe() as one, fan.subscribe() as two:
            assert changed(await take(one, 2)) == [(A, "update"), (B, "update")]
            assert changed(await take(two, 2)) == [(A, "update"), (B, "update")]

    run(scenario())


def test_stamps_every_notice_with_when_the_gateway_learned_of_it() -> None:
    events = ScriptedEvents(Attempt(batches=[[change(A)]], holds=True))
    fan = fanout(events)

    async def scenario() -> None:
        async with running(fan), fan.subscribe() as subscriber:
            started = dt.datetime.now(tz=dt.UTC)
            (notice,) = await take(subscriber, 1)

        assert notice.received.tzinfo is not None
        assert notice.received >= started - dt.timedelta(seconds=5)

    run(scenario())


def test_a_subscriber_that_leaves_is_not_kept_up_with() -> None:
    """Unsubscribing has to actually let go, or a stream is a slow leak."""
    fan = fanout()

    async def scenario() -> None:
        async with running(fan):
            async with fan.subscribe():
                assert fan.subscribers == 1
            assert fan.subscribers == 0

    run(scenario())


# Filtering


def test_only_delivers_the_resources_a_subscriber_asked_for() -> None:
    events = ScriptedEvents(Attempt(batches=[[change(A), change(B)]], holds=True))
    fan = fanout(events)

    async def scenario() -> None:
        async with (
            running(fan),
            fan.subscribe(Filter(resource_ids=frozenset({B}))) as subscriber,
        ):
            assert changed(await take(subscriber, 1)) == [(B, "update")]
            assert await nothing_more(subscriber)

    run(scenario())


def test_only_delivers_the_resource_types_a_subscriber_asked_for() -> None:
    events = ScriptedEvents(
        Attempt(batches=[[change(A, type_="grouped_light"), change(B)]], holds=True)
    )
    fan = fanout(events)

    async def scenario() -> None:
        wanted = Filter(resource_types=frozenset({"light"}))
        async with running(fan), fan.subscribe(wanted) as subscriber:
            assert changed(await take(subscriber, 1)) == [(B, "update")]

    run(scenario())


def test_a_gap_reaches_a_subscriber_that_filtered_everything_else_out() -> None:
    """A filter narrows what a subscriber sees, never what it is told.

    A Gap is about the stream, not about a Resource. A subscriber watching one
    light that is never told the connection dropped has been misled about the
    one thing it is watching.
    """
    events = ScriptedEvents(Attempt(), Attempt(holds=True))
    fan = fanout(events)

    async def scenario() -> None:
        wanted = Filter(resource_ids=frozenset({"a light nothing happens to"}))
        async with running(fan), fan.subscribe(wanted) as subscriber:
            (notice,) = await take(subscriber, 1)

        assert isinstance(notice, Gap)
        assert notice.cause is Cause.RECONNECTED

    run(scenario())


# Reconnecting


def test_says_nothing_about_a_gap_on_the_first_connection() -> None:
    """Subscribing is not a Gap: nothing was missed before the stream began."""
    events = ScriptedEvents(Attempt(batches=[[change(A)]], holds=True))
    fan = fanout(events)

    async def scenario() -> None:
        async with running(fan), fan.subscribe() as subscriber:
            (notice,) = await take(subscriber, 1)

        assert isinstance(notice, Change)

    run(scenario())


def test_announces_a_gap_on_every_reconnect() -> None:
    """Unconditionally: whether or not anything happened while it was gone."""
    events = ScriptedEvents(Attempt(), Attempt(), Attempt(holds=True))
    fan = fanout(events)

    async def scenario() -> None:
        async with running(fan), fan.subscribe() as subscriber:
            notices = await take(subscriber, 2)

        assert [notice.cause for notice in notices if isinstance(notice, Gap)] == [
            Cause.RECONNECTED,
            Cause.RECONNECTED,
        ]

    run(scenario())


def test_keeps_trying_a_bridge_it_cannot_read() -> None:
    """Every way the stream can fail is the same failure: reconnect.

    A Bridge that is not there and a Bridge sending frames that cannot be
    parsed both end the connection, and both are recovered from by opening it
    again — which announces a Gap and resyncs.
    """
    events = ScriptedEvents(
        Attempt(fails=BridgeUnreachableError("no route to host")),
        Attempt(fails=MalformedResponseError("that frame was not JSON")),
        Attempt(batches=[[change(A)]], holds=True),
    )
    fan = fanout(events)

    async def scenario() -> None:
        async with running(fan), fan.subscribe() as subscriber:
            assert changed(await take(subscriber, 1)) == [(A, "update")]

        assert events.connections == 3

    run(scenario())


def test_waits_longer_after_each_failure_and_then_stops_getting_longer() -> None:
    schedule = Backoff(base=0.5, ceiling=4.0, jitter=lambda ceiling: ceiling)
    delays = schedule.delays()

    assert [next(delays) for _ in range(6)] == [0.5, 1.0, 2.0, 4.0, 4.0, 4.0]


def test_draws_each_wait_from_below_its_ceiling() -> None:
    """Jittered, so every gateway on a network does not come back at once."""
    schedule = Backoff(base=1.0, ceiling=8.0)
    delays = schedule.delays()

    drawn = [next(delays) for _ in range(20)]

    assert all(0.0 <= delay <= 8.0 for delay in drawn)
    assert len(set(drawn)) > 1


# Resync


def test_resyncs_what_changed_while_the_connection_was_gone() -> None:
    events = ScriptedEvents(Attempt(), Attempt(holds=True))
    lights = ScriptedLights(
        [light(A, on={"on": True}), light(B, on={"on": True})],
        [light(A, on={"on": False}), light("c", on={"on": True})],
    )
    fan = fanout(events, lights)

    async def scenario() -> None:
        async with running(fan), fan.subscribe() as subscriber:
            gap, *rest = await take(subscriber, 4)

        assert isinstance(gap, Gap)
        assert sorted(changed(rest)) == sorted(
            [(A, "update"), (B, "delete"), ("c", "add")]
        )
        assert [
            notice.event.resource
            for notice in rest
            if isinstance(notice, Change) and notice.event.resource_id == A
        ] == [{"id": A, "type": "light", "on": {"on": False}}]

    run(scenario())


def test_a_resync_says_nothing_about_a_light_that_did_not_change() -> None:
    events = ScriptedEvents(Attempt(), Attempt(holds=True))
    lights = ScriptedLights([light(A, on={"on": True})], [light(A, on={"on": True})])
    fan = fanout(events, lights)

    async def scenario() -> None:
        async with running(fan), fan.subscribe() as subscriber:
            (gap,) = await take(subscriber, 1)
            assert isinstance(gap, Gap)
            assert await nothing_more(subscriber)

    run(scenario())


def test_a_resync_does_not_repeat_what_the_stream_already_carried() -> None:
    """The snapshot follows the events, so a Resync reports only the news."""
    events = ScriptedEvents(
        Attempt(batches=[[change(A, on={"on": False})]]), Attempt(holds=True)
    )
    lights = ScriptedLights([light(A, on={"on": True})], [light(A, on={"on": False})])
    fan = fanout(events, lights)

    async def scenario() -> None:
        async with running(fan), fan.subscribe() as subscriber:
            first, gap = await take(subscriber, 2)
            assert isinstance(first, Change)
            assert isinstance(gap, Gap)
            assert await nothing_more(subscriber)

    run(scenario())


def test_a_resync_that_cannot_read_the_lights_leaves_the_stream_up() -> None:
    """The Gap was announced either way; losing the stream too helps nobody."""
    events = ScriptedEvents(Attempt(), Attempt(batches=[[change(A)]], holds=True))
    lights = ScriptedLights(
        [light(A, on={"on": True})], BridgeUnreachableError("connection reset")
    )
    fan = fanout(events, lights)

    async def scenario() -> None:
        async with running(fan), fan.subscribe() as subscriber:
            gap, delivered = await take(subscriber, 2)

        assert isinstance(gap, Gap)
        assert changed([delivered]) == [(A, "update")]

    run(scenario())


def test_a_resync_ignores_resources_the_gateway_does_not_model() -> None:
    """Only lights are read back, so only lights can be resynced.

    A grouped light's event still reaches subscribers; what it does not do is
    make the snapshot claim to know something the Resync cannot check.
    """
    events = ScriptedEvents(
        Attempt(batches=[[change(B, type_="grouped_light")]]), Attempt(holds=True)
    )
    lights = ScriptedLights([light(A)], [light(A)])
    fan = fanout(events, lights)

    async def scenario() -> None:
        async with running(fan), fan.subscribe() as subscriber:
            first, gap = await take(subscriber, 2)
            assert changed([first]) == [(B, "update")]
            assert isinstance(gap, Gap)
            assert await nothing_more(subscriber)

    run(scenario())


# Subscribers that cannot keep up


def test_a_slow_subscriber_does_not_hold_up_the_reader() -> None:
    """The whole point of the queues: one client cannot stall the Bridge.

    Nobody reads this subscription at all, and its queue holds one. The
    reader still takes everything the Bridge sent.
    """
    events = ScriptedEvents(
        Attempt(batches=[[change(A)], [change(B)], [change("c")]], holds=True)
    )
    fan = fanout(events, queue_size=1)

    async def scenario() -> None:
        async with running(fan), fan.subscribe():
            await asyncio.wait_for(events.drained.wait(), 2)

    run(scenario())


def test_tells_a_slow_subscriber_how_many_it_missed() -> None:
    """In the position they would have been: after what it did receive."""
    events = ScriptedEvents(
        Attempt(
            batches=[[change(A)], [change(B)], [change("c")], [change("d")]],
            holds=True,
        )
    )
    fan = fanout(events, queue_size=2)

    async def scenario() -> None:
        async with running(fan), fan.subscribe() as slow:
            await asyncio.wait_for(events.drained.wait(), 2)
            first, second, gap = await take(slow, 3)

        assert changed([first, second]) == [(A, "update"), (B, "update")]
        assert isinstance(gap, Gap)
        assert gap.cause is Cause.SUBSCRIBER_BEHIND
        assert gap.missed == 2

    run(scenario())


def test_a_subscriber_that_caught_up_is_told_once() -> None:
    """One Gap for the run of events it missed, not one for each of them."""
    events = ScriptedEvents(
        Attempt(batches=[[change(A)], [change(B)], [change("c")]], holds=True)
    )
    fan = fanout(events, queue_size=2)

    async def scenario() -> None:
        async with running(fan), fan.subscribe() as slow:
            await asyncio.wait_for(events.drained.wait(), 2)
            notices = await take(slow, 3)
            assert await nothing_more(slow)

        assert changed(notices[:2]) == [(A, "update"), (B, "update")]
        assert isinstance(notices[2], Gap)
        assert notices[2].missed == 1

    run(scenario())
