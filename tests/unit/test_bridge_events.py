"""The Bridge's server-sent event stream, read off a real socket.

Every test here goes through the verified transport to a TLS listener that
writes actual `text/event-stream` bytes, because what this module does is read
a byte stream: a frame split across two writes, a keep-alive comment, and a
connection that cuts a frame in half are all things that happen to bytes.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable

import pytest
from conftest import BRIDGE_ID, BridgeCerts, FakeBridge, run

from hue_grpc.hue.events import EVENT_STREAM, BridgeEvent, BridgeEvents
from hue_grpc.hue.transport import (
    DEFAULT_TIMEOUTS,
    BridgeResponseError,
    HueTransport,
    MalformedResponseError,
    Timeouts,
)

LIGHT_ID = "8f2a1e00-0000-4000-8000-000000000001"
OTHER_ID = "8f2a1e00-0000-4000-8000-000000000002"

Responder = Callable[[bytes, asyncio.StreamWriter], Awaitable[None]]

#: One frame carrying two Bridge events, the first about two Resources at
#: once. The Bridge batches like this constantly: one dimmer press moves a
#: light and the group it belongs to, and both arrive together.
TWO_EVENTS = (
    'data: [{"id": "e1", "type": "update", '
    '"creationtime": "2026-02-06T02:09:13Z", "data": ['
    f'{{"type": "light", "id": "{LIGHT_ID}", "on": {{"on": false}}}}, '
    f'{{"type": "grouped_light", "id": "{OTHER_ID}", '
    '"dimming": {"brightness": 66.8}}]}, '
    '{"id": "e2", "type": "delete", '
    '"creationtime": "2026-02-06T02:09:14Z", "data": ['
    f'{{"type": "light", "id": "{LIGHT_ID}"}}]}}]\n\n'
)

ONE_LIGHT = (
    'data: [{"id": "e1", "type": "update", "data": [{"type": "light", "id": "a"}]}]\n\n'
)


def writes(*frames: str) -> Responder:
    """A Bridge that answers the event stream with `frames`, then hangs up."""

    async def respond(request: bytes, writer: asyncio.StreamWriter) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Connection: close\r\n\r\n"
        )
        for frame in frames:
            writer.write(frame.encode())
            await writer.drain()
        writer.close()

    return respond


def events_of(
    bridge: FakeBridge, certs: BridgeCerts, *, timeouts: Timeouts = DEFAULT_TIMEOUTS
) -> BridgeEvents:
    return BridgeEvents(
        HueTransport(
            bridge_id=BRIDGE_ID,
            address=bridge.address,
            application_key="an-application-key",
            ca_pem=certs.ca_pem,
            timeouts=timeouts,
        )
    )


async def read(events: BridgeEvents) -> list[list[BridgeEvent]]:
    """Every batch the stream delivers before it ends."""
    async with events.transport, events.connected() as batches:
        return [batch async for batch in batches]


def batches_from(certs: BridgeCerts, *frames: str) -> list[list[BridgeEvent]]:
    async def scenario() -> list[list[BridgeEvent]]:
        async with FakeBridge(certs, respond=writes(*frames)) as bridge:
            return await read(events_of(bridge, certs))

    return run(scenario())


# Parsing and batching


def test_reads_one_frame_as_a_batch_of_resource_changes(
    bridge_certs: BridgeCerts,
) -> None:
    """One frame, two Bridge events, three Resources: one batch of three."""
    batches = batches_from(bridge_certs, TWO_EVENTS)

    assert len(batches) == 1
    first, second, third = batches[0]
    assert (first.id, first.type, first.resource_id) == ("e1", "update", LIGHT_ID)
    assert first.resource["on"] == {"on": False}
    assert (second.id, second.resource_type, second.resource_id) == (
        "e1",
        "grouped_light",
        OTHER_ID,
    )
    assert (third.id, third.type, third.resource_id) == ("e2", "delete", LIGHT_ID)


def test_delivers_each_frame_as_it_arrives(bridge_certs: BridgeCerts) -> None:
    """Frames are separate batches, not one list accumulated at the end."""
    second = ONE_LIGHT.replace('"e1"', '"e2"')

    batches = batches_from(bridge_certs, ONE_LIGHT, second)

    assert [[event.id for event in batch] for batch in batches] == [["e1"], ["e2"]]


def test_ignores_the_lines_that_are_not_data(bridge_certs: BridgeCerts) -> None:
    """An id, an event name and a keep-alive comment are all not events.

    The Bridge sends `id:` on every frame and comments to hold the connection
    open through a quiet hour. Neither is something to hand a subscriber.
    """
    frame = f": hi\nid: 1770343753:0\nevent: update\n{ONE_LIGHT}: hi\n"

    batches = batches_from(bridge_certs, frame)

    assert [[event.id for event in batch] for batch in batches] == [["e1"]]


def test_joins_a_data_field_split_across_lines(bridge_certs: BridgeCerts) -> None:
    """Server-sent events allow one payload over several `data:` lines."""
    frame = (
        'data: [{"id": "e1", "type": "update",\n'
        'data:  "data": [{"type": "light", "id": "a"}]}]\n'
        "\n"
    )

    batches = batches_from(bridge_certs, frame)

    assert [[event.resource_id for event in batch] for batch in batches] == [["a"]]


def test_does_not_deliver_a_frame_the_connection_cut_in_half(
    bridge_certs: BridgeCerts,
) -> None:
    """Half a frame is not an event; it is truncated JSON."""
    half = 'data: [{"id": "e3", "type": "update", "data": [{"type": "li'

    batches = batches_from(bridge_certs, TWO_EVENTS, half)

    assert [[event.id for event in batch] for batch in batches] == [["e1", "e1", "e2"]]


def test_keeps_the_bridges_own_timestamp(bridge_certs: BridgeCerts) -> None:
    batches = batches_from(bridge_certs, TWO_EVENTS)

    assert batches[0][0].created == dt.datetime(2026, 2, 6, 2, 9, 13, tzinfo=dt.UTC)


def test_reads_an_event_the_bridge_did_not_timestamp(
    bridge_certs: BridgeCerts,
) -> None:
    """An unreadable creationtime is not a reason to lose the event.

    The Gateway's own receive time is what a subscriber can always rely on;
    the Bridge's is extra.
    """
    frame = (
        'data: [{"id": "e1", "type": "update", "creationtime": "whenever", '
        '"data": [{"type": "light", "id": "a"}]}]\n\n'
    )

    batches = batches_from(bridge_certs, frame)

    assert batches[0][0].created is None
    assert batches[0][0].resource_id == "a"


# Frames that are not events


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        '{"id": "e1", "type": "update", "data": []}',
        "[[]]",
        '[{"id": "e1", "type": "update", "data": {"type": "light"}}]',
        '[{"id": "e1", "type": "update", "data": ["a light"]}]',
    ],
)
def test_refuses_a_frame_that_is_not_a_list_of_events(
    bridge_certs: BridgeCerts, payload: str
) -> None:
    """A frame the Gateway cannot read is a Gap, not something to skip past.

    Skipping it would drop events with nobody told. Failing ends the
    connection, and every reconnect announces a Gap and resyncs.
    """

    async def scenario() -> None:
        async with FakeBridge(
            bridge_certs, respond=writes(f"data: {payload}\n\n")
        ) as bridge:
            with pytest.raises(MalformedResponseError):
                await read(events_of(bridge, bridge_certs))

    run(scenario())


def test_reports_a_bridge_that_refuses_the_stream(bridge_certs: BridgeCerts) -> None:
    """A revoked Application Key is a failed exchange, not an empty stream."""

    async def scenario() -> None:
        async with FakeBridge(
            bridge_certs,
            body='{"errors": [{"description": "unauthorized user"}]}',
            status="401 Unauthorized",
        ) as bridge:
            with pytest.raises(BridgeResponseError) as raised:
                await read(events_of(bridge, bridge_certs))

        assert raised.value.status_code == 401

    run(scenario())


# What the Bridge is asked for


def test_asks_for_the_event_stream_with_the_application_key(
    bridge_certs: BridgeCerts,
) -> None:
    async def scenario() -> None:
        async with FakeBridge(bridge_certs, respond=writes(TWO_EVENTS)) as bridge:
            await read(events_of(bridge, bridge_certs))

        request = bridge.requests[0].decode()
        assert f"GET {EVENT_STREAM} " in request
        assert "hue-application-key: an-application-key" in request.lower()
        assert "accept: text/event-stream" in request.lower()

    run(scenario())


def test_stays_open_through_a_silence_longer_than_the_read_timeout(
    bridge_certs: BridgeCerts,
) -> None:
    """The Bridge is silent between events, and silence is not a timeout."""

    async def dribble(request: bytes, writer: asyncio.StreamWriter) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        await asyncio.sleep(0.3)
        writer.write(TWO_EVENTS.encode())
        await writer.drain()
        writer.close()

    async def scenario() -> None:
        async with FakeBridge(bridge_certs, respond=dribble) as bridge:
            batches = await read(
                events_of(bridge, bridge_certs, timeouts=Timeouts(read=0.05))
            )

        assert [event.id for event in batches[0]] == ["e1", "e1", "e2"]

    run(scenario())
