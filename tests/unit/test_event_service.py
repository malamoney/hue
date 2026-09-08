"""`hue.v1.EventService`, called the way a client calls it.

The whole way: a gRPC channel to a listening gateway, through the interceptor
chain and the servicer, out to a TLS listener presenting a Bridge-shaped
certificate that serves real `text/event-stream` bytes and a real light
collection. The connection is dropped for the same reason a bridge drops one,
by closing the socket, because what a forced disconnect produces is the thing
this service exists to get right.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import grpc
import pytest
from conftest import BRIDGE_ID, BridgeCerts, FakeBridge, run

from hue.v1 import common_pb2, event_service_pb2, events_pb2
from hue.v1 import event_service_pb2_grpc as event_grpc
from hue_grpc.events.fanout import Backoff, EventFanout
from hue_grpc.events.service import SERVICE_NAME, hosted_event_service
from hue_grpc.hue.events import EVENT_STREAM, BridgeEvents
from hue_grpc.hue.lights import Lights
from hue_grpc.hue.transport import HueTransport
from hue_grpc.serving.config import GatewayConfig
from hue_grpc.serving.serve import running_gateway

A = "8f2a1e00-0000-4000-8000-00000000000a"
B = "8f2a1e00-0000-4000-8000-00000000000b"

#: The port is whatever is free, and shutdown is not what most of these are for.
EPHEMERAL = GatewayConfig(port=0, shutdown_drain=0)

#: No waiting between connections: `test_event_fanout` covers the schedule.
IMMEDIATELY = Backoff(base=0.0, ceiling=0.0)

SUBSCRIBE = event_service_pb2.SubscribeRequest


def frame(*events: str) -> str:
    return "data: [" + ", ".join(events) + "]\n\n"


def event(event_id: str, resource: str, *, kind: str = "update") -> str:
    return (
        f'{{"id": "{event_id}", "type": "{kind}", '
        f'"creationtime": "2026-02-06T02:09:13Z", "data": [{resource}]}}'
    )


def light(resource_id: str, **properties: object) -> str:
    return json.dumps({"id": resource_id, "type": "light", **properties})


def collection(*lights: str) -> str:
    return '{"errors": [], "data": [' + ", ".join(lights) + "]}"


class ScriptedBridge:
    """A Bridge answering both paths this service needs, from a script.

    `FakeBridge` hands every request to one responder, and this is it: the
    request line says whether the event stream or the light collection is
    being asked for. The first event stream connection stays open until
    `disconnect` is set, which is how a test forces the reconnect.
    """

    def __init__(self, *, reads: Sequence[str], streams: Sequence[Sequence[str]]):
        self._reads = reads
        self._streams = streams
        self.disconnect = asyncio.Event()
        self.connections = 0
        self.reads = 0

    async def respond(self, request: bytes, writer: asyncio.StreamWriter) -> None:
        if EVENT_STREAM.encode() in request.split(b"\r\n")[0]:
            await self._stream(writer)
        else:
            self._answer(writer, self._reads[min(self.reads, len(self._reads) - 1)])
            self.reads += 1

    async def _stream(self, writer: asyncio.StreamWriter) -> None:
        connection = self.connections
        self.connections += 1
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Connection: close\r\n\r\n"
        )
        for sent in self._streams[min(connection, len(self._streams) - 1)]:
            writer.write(sent.encode())
        await writer.drain()
        if connection == 0:
            await self.disconnect.wait()
            writer.close()
        else:
            await asyncio.sleep(3600)

    def _answer(self, writer: asyncio.StreamWriter, body: str) -> None:
        payload = body.encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + payload
        )


@asynccontextmanager
async def fanout_reading(
    bridge: FakeBridge, certs: BridgeCerts
) -> AsyncIterator[EventFanout]:
    """A fanout whose stream and Resyncs both go to `bridge`.

    One transport for both, as the gateway wires it: a Bridge is one host and
    one connection pool, whichever of its paths is being asked for.
    """
    transport = HueTransport(
        bridge_id=BRIDGE_ID,
        address=bridge.address,
        application_key="an-application-key",
        ca_pem=certs.ca_pem,
    )
    async with transport:
        yield EventFanout(
            events=BridgeEvents(transport),
            lights=Lights(transport),
            backoff=IMMEDIATELY,
        )


@asynccontextmanager
async def gateway_for(
    fanout: EventFanout | None, *, config: GatewayConfig = EPHEMERAL
) -> AsyncIterator[event_grpc.EventServiceStub]:
    hosted = hosted_event_service(fanout, bridge_id=BRIDGE_ID)
    async with (
        running_gateway(config, [hosted]) as gateway,
        grpc.aio.insecure_channel(f"127.0.0.1:{gateway.port}") as channel,
    ):
        yield event_grpc.EventServiceStub(channel)


@asynccontextmanager
async def subscribing(
    script: ScriptedBridge, certs: BridgeCerts
) -> AsyncIterator[tuple[EventFanout, event_grpc.EventServiceStub]]:
    """A gateway reading `script`'s Bridge, and a stub dialled at it."""
    async with (
        FakeBridge(certs, respond=script.respond) as bridge,
        fanout_reading(bridge, certs) as fanout,
        gateway_for(fanout) as stub,
    ):
        yield fanout, stub


async def next_event(
    stream: grpc.aio.UnaryStreamCall,
) -> event_service_pb2.HueEvent:
    """The next message, or a failed test rather than a hung one."""
    received = await asyncio.wait_for(stream.read(), 5)
    assert isinstance(received, event_service_pb2.HueEvent)
    return received


# Events flowing


def test_streams_what_the_bridge_reports(bridge_certs: BridgeCerts) -> None:
    script = ScriptedBridge(
        reads=[collection(light(A, on={"on": True}))],
        streams=[
            [
                frame(
                    event(
                        "e1", light(A, on={"on": False}, dimming={"brightness": 60.0})
                    )
                )
            ]
        ],
    )

    async def scenario() -> None:
        async with subscribing(script, bridge_certs) as (_, stub):
            received = await next_event(stub.Subscribe(SUBSCRIBE()))

        assert received.bridge_id == BRIDGE_ID
        assert received.gateway_time.seconds > 0
        change = received.change
        assert change.event_id == "e1"
        assert change.type == events_pb2.Event.TYPE_UPDATE
        assert change.resource.rid == A
        assert change.resource.rtype == common_pb2.ResourceIdentifier.RTYPE_LIGHT
        assert change.bridge_time.ToDatetime().isoformat() == "2026-02-06T02:09:13"
        assert change.light.on.on is False
        assert change.light.dimming.brightness == 60.0

    run(scenario())


def test_passes_on_a_resource_it_does_not_model(bridge_certs: BridgeCerts) -> None:
    """A grouped light is not a Light, and is still worth telling a client about."""
    grouped = json.dumps({"id": B, "type": "grouped_light", "on": {"on": True}})
    script = ScriptedBridge(
        reads=[collection()], streams=[[frame(event("e1", grouped))]]
    )

    async def scenario() -> None:
        async with subscribing(script, bridge_certs) as (_, stub):
            received = await next_event(stub.Subscribe(SUBSCRIBE()))

        assert received.change.resource.rid == B
        assert received.change.resource.rtype == (
            common_pb2.ResourceIdentifier.RTYPE_GROUPED_LIGHT
        )
        assert received.change.WhichOneof("update") is None

    run(scenario())


def test_only_streams_the_resources_a_client_asked_for(
    bridge_certs: BridgeCerts,
) -> None:
    script = ScriptedBridge(
        reads=[collection()],
        streams=[[frame(event("e1", light(A)), event("e2", light(B)))]],
    )

    async def scenario() -> None:
        async with subscribing(script, bridge_certs) as (_, stub):
            received = await next_event(stub.Subscribe(SUBSCRIBE(resource_ids=[B])))

        assert received.change.resource.rid == B

    run(scenario())


# The forced disconnect


def test_a_lost_connection_is_a_gap_and_then_a_resync(
    bridge_certs: BridgeCerts,
) -> None:
    """The whole point, end to end.

    The client sees a live change, the Bridge hangs up, and what arrives next
    says both things that are true: events may have been missed, and here is
    what is different now. The brightness the live event already carried is
    not repeated — the Resync reports the news, not the state.
    """
    script = ScriptedBridge(
        reads=[
            collection(light(A, on={"on": True}, dimming={"brightness": 50.0})),
            collection(light(A, on={"on": False}, dimming={"brightness": 60.0})),
        ],
        streams=[[frame(event("e1", light(A, dimming={"brightness": 60.0})))], []],
    )

    async def scenario() -> None:
        async with subscribing(script, bridge_certs) as (_, stub):
            stream = stub.Subscribe(SUBSCRIBE())
            live = await next_event(stream)
            assert live.change.light.dimming.brightness == 60.0

            script.disconnect.set()

            gap = await next_event(stream)
            resynced = await next_event(stream)

        assert gap.gap.cause == event_service_pb2.Gap.CAUSE_RECONNECTED
        assert gap.gap.missed == 0
        assert resynced.change.event_id == ""
        assert resynced.change.type == events_pb2.Event.TYPE_UPDATE
        assert resynced.change.resource.rid == A
        assert resynced.change.light.on.on is False
        assert not resynced.change.light.HasField("dimming")
        assert not resynced.change.HasField("bridge_time")

    run(scenario())


# Filters that cannot be served


def test_refuses_a_filter_on_no_type_in_particular(bridge_certs: BridgeCerts) -> None:
    script = ScriptedBridge(reads=[collection()], streams=[[]])

    async def scenario() -> None:
        async with subscribing(script, bridge_certs) as (_, stub):
            unspecified = common_pb2.ResourceIdentifier.RTYPE_UNSPECIFIED
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stub.Subscribe(SUBSCRIBE(resource_types=[unspecified])).read()

        assert raised.value.code() is grpc.StatusCode.INVALID_ARGUMENT
        assert "RTYPE_UNSPECIFIED" in raised.value.details()

    run(scenario())


def test_refuses_a_filter_on_an_empty_resource_id(bridge_certs: BridgeCerts) -> None:
    script = ScriptedBridge(reads=[collection()], streams=[[]])

    async def scenario() -> None:
        async with subscribing(script, bridge_certs) as (_, stub):
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stub.Subscribe(SUBSCRIBE(resource_ids=[""])).read()

        assert raised.value.code() is grpc.StatusCode.INVALID_ARGUMENT

    run(scenario())


# A gateway with no bridge


def test_tells_an_unpaired_gateways_clients_what_to_do() -> None:
    async def scenario() -> None:
        async with gateway_for(None) as stub:
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stub.Subscribe(SUBSCRIBE()).read()

        assert raised.value.code() is grpc.StatusCode.FAILED_PRECONDITION
        assert "pair" in raised.value.details()

    run(scenario())


def test_is_hosted_under_its_own_name() -> None:
    assert SERVICE_NAME == "hue.v1.EventService"


# Endings


def test_a_client_that_hangs_up_lets_its_queue_go(bridge_certs: BridgeCerts) -> None:
    """A subscription that outlived its call would be a leak per client."""
    script = ScriptedBridge(
        reads=[collection()], streams=[[frame(event("e1", light(A)))]]
    )

    async def scenario() -> None:
        async with subscribing(script, bridge_certs) as (fanout, stub):
            stream = stub.Subscribe(SUBSCRIBE())
            await next_event(stream)
            assert fanout.subscribers == 1

            stream.cancel()
            for _ in range(100):
                await asyncio.sleep(0.01)
                if fanout.subscribers == 0:
                    break

            assert fanout.subscribers == 0

    run(scenario())


def test_shutting_down_ends_a_stream_rather_than_stranding_it(
    bridge_certs: BridgeCerts,
) -> None:
    script = ScriptedBridge(
        reads=[collection()], streams=[[frame(event("e1", light(A)))]]
    )
    stopping = GatewayConfig(port=0, shutdown_drain=0, shutdown_grace=0)

    async def scenario() -> None:
        async with (
            FakeBridge(bridge_certs, respond=script.respond) as bridge,
            fanout_reading(bridge, bridge_certs) as fanout,
        ):
            hosted = hosted_event_service(fanout, bridge_id=BRIDGE_ID)
            # The channel outlives the gateway on purpose: a client whose own
            # channel closed first would be told it cancelled the call, which
            # says nothing about what the gateway did with it.
            async with running_gateway(stopping, [hosted]) as gateway:
                channel = grpc.aio.insecure_channel(f"127.0.0.1:{gateway.port}")
                stream = event_grpc.EventServiceStub(channel).Subscribe(SUBSCRIBE())
                await next_event(stream)

            try:
                assert await asyncio.wait_for(_ended(stream), 5)
            finally:
                await channel.close()

    run(scenario())


async def _ended(stream: grpc.aio.UnaryStreamCall) -> bool:
    """Whether the stream is over, however the gateway chose to end it."""
    try:
        return await stream.read() is grpc.aio.EOF
    except grpc.aio.AioRpcError:
        return True
