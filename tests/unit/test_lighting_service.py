"""`hue.v1.LightingService`, called the way a client calls it.

Every test here goes the whole way: a gRPC channel to a listening gateway,
through the interceptor chain and the servicer, out over the verified
transport to a TLS listener presenting a Bridge-shaped certificate. What the
Bridge received is asserted on the bytes it received, because "an omitted
field leaves the light alone" is a claim about the request that was sent, not
about the object that produced it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import grpc
import pytest
from conftest import BRIDGE_ID, BridgeCerts, FakeBridge, run
from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc

from hue.v1 import common_pb2, lighting_pb2, lighting_service_pb2
from hue.v1 import lighting_service_pb2_grpc as lighting_grpc
from hue_grpc.hue.lights import Lights
from hue_grpc.hue.transport import HueTransport
from hue_grpc.lighting.service import SERVICE_NAME, hosted_lighting_service
from hue_grpc.serving.config import GatewayConfig
from hue_grpc.serving.serve import running_gateway

LIGHT_ID = "8f2a1e00-0000-4000-8000-000000000001"
OTHER_ID = "8f2a1e00-0000-4000-8000-000000000002"

#: A colour light and a dimmable-only one, so that "unset" has something to
#: mean in the answer.
TWO_LIGHTS = json.dumps(
    {
        "errors": [],
        "data": [
            {
                "type": "light",
                "id": LIGHT_ID,
                "metadata": {"name": "Desk", "archetype": "sultan_bulb"},
                "on": {"on": True},
                "dimming": {"brightness": 42.5, "min_dim_level": 0.2},
                "color": {"xy": {"x": 0.45, "y": 0.41}, "gamut_type": "C"},
                "mode": "normal",
            },
            {
                "type": "light",
                "id": OTHER_ID,
                "metadata": {"name": "Hall", "archetype": "plug"},
                "on": {"on": False},
            },
        ],
    }
)

CHANGED = json.dumps({"errors": [], "data": [{"rid": LIGHT_ID, "rtype": "light"}]})

#: The port is whatever is free, and shutdown is not what these tests are for.
EPHEMERAL = GatewayConfig(port=0, shutdown_drain=0)


def transport_to(bridge: FakeBridge, certs: BridgeCerts) -> HueTransport:
    return HueTransport(
        bridge_id=BRIDGE_ID,
        address=bridge.address,
        application_key="an-application-key",
        ca_pem=certs.ca_pem,
    )


@asynccontextmanager
async def gateway_for(
    lights: Lights | None,
) -> AsyncIterator[lighting_grpc.LightingServiceStub]:
    """A listening gateway serving `lights`, and a stub dialled at it."""
    async with (
        running_gateway(EPHEMERAL, [hosted_lighting_service(lights)]) as gateway,
        grpc.aio.insecure_channel(f"127.0.0.1:{gateway.port}") as channel,
    ):
        yield lighting_grpc.LightingServiceStub(channel)


@asynccontextmanager
async def serving(
    bridge: FakeBridge, certs: BridgeCerts
) -> AsyncIterator[lighting_grpc.LightingServiceStub]:
    lights = Lights(transport_to(bridge, certs))
    async with lights.transport, gateway_for(lights) as stub:
        yield stub


async def hangs_up_on_everything(request: bytes, writer: asyncio.StreamWriter) -> None:
    """A bridge that accepts the connection and drops it without answering."""
    writer.close()


def request_body(bridge: FakeBridge) -> object:
    _, _, body = bridge.requests[0].decode().partition("\r\n\r\n")
    return json.loads(body)


# Reading


def test_lists_every_light_the_bridge_has(bridge_certs: BridgeCerts) -> None:
    async def scenario() -> None:
        async with (
            FakeBridge(bridge_certs, body=TWO_LIGHTS) as bridge,
            serving(bridge, bridge_certs) as stub,
        ):
            response = await stub.ListLights(lighting_service_pb2.ListLightsRequest())

        assert [light.id for light in response.lights] == [LIGHT_ID, OTHER_ID]
        assert response.lights[0].metadata.name == "Desk"
        assert response.lights[0].color.gamut_type == (
            lighting_pb2.LightGet.Color.GAMUT_TYPE_C
        )

    run(scenario())


def test_a_light_without_colour_does_not_appear_to_have_black(
    bridge_certs: BridgeCerts,
) -> None:
    async def scenario() -> None:
        async with (
            FakeBridge(bridge_certs, body=TWO_LIGHTS) as bridge,
            serving(bridge, bridge_certs) as stub,
        ):
            response = await stub.ListLights(lighting_service_pb2.ListLightsRequest())

        plug = response.lights[1]
        assert not plug.HasField("color")
        assert not plug.HasField("dimming")
        assert plug.on.on is False

    run(scenario())


def test_reads_one_light(bridge_certs: BridgeCerts) -> None:
    async def scenario() -> None:
        async with (
            FakeBridge(bridge_certs, body=TWO_LIGHTS) as bridge,
            serving(bridge, bridge_certs) as stub,
        ):
            light = await stub.GetLight(
                lighting_service_pb2.GetLightRequest(light_id=LIGHT_ID)
            )

        assert light.id == LIGHT_ID
        assert light.dimming.brightness == 42.5
        assert (
            bridge.requests[0]
            .decode()
            .startswith(f"GET /clip/v2/resource/light/{LIGHT_ID} HTTP/1.1")
        )

    run(scenario())


def test_a_light_the_bridge_does_not_have_is_not_found(
    bridge_certs: BridgeCerts,
) -> None:
    body = json.dumps({"errors": [{"description": "resource not available"}]})

    async def scenario() -> None:
        async with (
            FakeBridge(bridge_certs, body=body, status="404 Not Found") as bridge,
            serving(bridge, bridge_certs) as stub,
        ):
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stub.GetLight(
                    lighting_service_pb2.GetLightRequest(light_id=LIGHT_ID)
                )

        assert raised.value.code() == grpc.StatusCode.NOT_FOUND

    run(scenario())


def test_an_id_that_could_steer_the_request_is_refused_here(
    bridge_certs: BridgeCerts,
) -> None:
    async def scenario() -> None:
        async with (
            FakeBridge(bridge_certs, body=TWO_LIGHTS) as bridge,
            serving(bridge, bridge_certs) as stub,
        ):
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stub.GetLight(
                    lighting_service_pb2.GetLightRequest(light_id="../bridge")
                )

            assert bridge.requests == []
        assert raised.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    run(scenario())


def test_an_unreadable_answer_is_the_gateway_s_problem_not_the_client_s(
    bridge_certs: BridgeCerts,
) -> None:
    body = json.dumps({"errors": [], "data": [{"id": LIGHT_ID, "dimming": "half"}]})

    async def scenario() -> None:
        async with (
            FakeBridge(bridge_certs, body=body) as bridge,
            serving(bridge, bridge_certs) as stub,
        ):
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stub.GetLight(
                    lighting_service_pb2.GetLightRequest(light_id=LIGHT_ID)
                )

        assert raised.value.code() == grpc.StatusCode.INTERNAL
        assert "LightGet.dimming" in raised.value.details()

    run(scenario())


def test_a_bridge_that_cannot_be_reached_is_unavailable() -> None:
    async def scenario() -> None:
        # Port 1: nothing listens there, and nothing is expected to.
        lights = Lights(HueTransport(bridge_id=BRIDGE_ID, address="127.0.0.1:1"))
        async with lights.transport, gateway_for(lights) as stub:
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stub.ListLights(lighting_service_pb2.ListLightsRequest())

        assert raised.value.code() == grpc.StatusCode.UNAVAILABLE

    run(scenario())


# Changing


def test_a_command_sends_only_what_was_set(bridge_certs: BridgeCerts) -> None:
    """The acceptance criterion, asserted on what the bridge received."""
    command = lighting_pb2.LightPut()
    command.on.on = False

    async def scenario() -> None:
        async with (
            FakeBridge(bridge_certs, body=CHANGED) as bridge,
            serving(bridge, bridge_certs) as stub,
        ):
            response = await stub.UpdateLight(
                lighting_service_pb2.UpdateLightRequest(
                    light_id=LIGHT_ID, command=command
                )
            )

        assert request_body(bridge) == {"on": {"on": False}}
        assert (
            bridge.requests[0]
            .decode()
            .startswith(f"PUT /clip/v2/resource/light/{LIGHT_ID} HTTP/1.1")
        )
        assert [resource.rid for resource in response.updated] == [LIGHT_ID]
        assert response.updated[0].rtype == common_pb2.ResourceIdentifier.RTYPE_LIGHT
        assert list(response.errors) == []

    run(scenario())


def test_dimming_a_light_says_nothing_about_its_power(
    bridge_certs: BridgeCerts,
) -> None:
    command = lighting_pb2.LightPut()
    command.dimming.brightness = 0

    async def scenario() -> None:
        async with (
            FakeBridge(bridge_certs, body=CHANGED) as bridge,
            serving(bridge, bridge_certs) as stub,
        ):
            await stub.UpdateLight(
                lighting_service_pb2.UpdateLightRequest(
                    light_id=LIGHT_ID, command=command
                )
            )

        # Brightness zero, and not a word about `on`: dimming to nothing and
        # switching off are different requests.
        assert request_body(bridge) == {"dimming": {"brightness": 0.0}}

    run(scenario())


def test_a_partly_refused_change_arrives_whole(bridge_certs: BridgeCerts) -> None:
    """Hue's own errors ride in the response; the RPC still succeeded."""
    body = json.dumps(
        {
            "errors": [{"description": "device (light) has communication issues"}],
            "data": [{"rid": LIGHT_ID, "rtype": "light"}],
        }
    )
    command = lighting_pb2.LightPut()
    command.on.on = True

    async def scenario() -> None:
        async with (
            FakeBridge(bridge_certs, body=body) as bridge,
            serving(bridge, bridge_certs) as stub,
        ):
            response = await stub.UpdateLight(
                lighting_service_pb2.UpdateLightRequest(
                    light_id=LIGHT_ID, command=command
                )
            )

        # Intact means both halves and every field of them: what the bridge
        # changed is still identified, and what it refused is still in the
        # bridge's own words.
        assert len(response.updated) == 1
        assert response.updated[0].rid == LIGHT_ID
        assert response.updated[0].rtype == common_pb2.ResourceIdentifier.RTYPE_LIGHT
        assert list(response.errors) == [
            common_pb2.Error(description="device (light) has communication issues")
        ]

    run(scenario())


def test_a_value_hue_would_reject_never_leaves_the_gateway(
    bridge_certs: BridgeCerts,
) -> None:
    command = lighting_pb2.LightPut()
    command.dimming.brightness = 140

    async def scenario() -> None:
        async with (
            FakeBridge(bridge_certs, body=CHANGED) as bridge,
            serving(bridge, bridge_certs) as stub,
        ):
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stub.UpdateLight(
                    lighting_service_pb2.UpdateLightRequest(
                        light_id=LIGHT_ID, command=command
                    )
                )

            assert bridge.requests == []
        assert raised.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        assert "Dimming.brightness" in raised.value.details()

    run(scenario())


def test_a_command_that_changes_nothing_is_refused(
    bridge_certs: BridgeCerts,
) -> None:
    async def scenario() -> None:
        async with (
            FakeBridge(bridge_certs, body=CHANGED) as bridge,
            serving(bridge, bridge_certs) as stub,
        ):
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stub.UpdateLight(
                    lighting_service_pb2.UpdateLightRequest(light_id=LIGHT_ID)
                )

            assert bridge.requests == []
        assert raised.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    run(scenario())


# What the bridge said


def test_what_the_bridge_refused_reaches_the_client_in_its_own_words(
    bridge_certs: BridgeCerts,
) -> None:
    """A refusal on a failed exchange is a status, and a status code alone
    cannot say which field to fix."""
    body = json.dumps(
        {"errors": [{"description": "invalid value, dimming.brightness, 101"}]}
    )
    command = lighting_pb2.LightPut()
    command.on.on = True

    async def scenario() -> None:
        async with (
            FakeBridge(bridge_certs, body=body, status="400 Bad Request") as bridge,
            serving(bridge, bridge_certs) as stub,
        ):
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stub.UpdateLight(
                    lighting_service_pb2.UpdateLightRequest(
                        light_id=LIGHT_ID, command=command
                    )
                )

        assert raised.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        assert "invalid value, dimming.brightness, 101" in raised.value.details()

    run(scenario())


def test_a_bridge_that_does_not_serve_the_path_is_unimplemented(
    bridge_certs: BridgeCerts,
) -> None:
    """Older firmware, not a fault and not something to retry."""

    async def scenario() -> None:
        async with (
            FakeBridge(bridge_certs, body="{}", status="501 Not Implemented") as bridge,
            serving(bridge, bridge_certs) as stub,
        ):
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stub.ListLights(lighting_service_pb2.ListLightsRequest())

        assert raised.value.code() == grpc.StatusCode.UNIMPLEMENTED

    run(scenario())


def test_a_lost_read_is_asked_again_and_a_lost_change_is_not(
    bridge_certs: BridgeCerts,
) -> None:
    """The whole stack, not just the transport: no layer above it re-sends a
    mutation the bridge may already have applied."""
    command = lighting_pb2.LightPut()
    command.on.on = True

    async def scenario() -> None:
        async with (
            FakeBridge(bridge_certs, respond=hangs_up_on_everything) as bridge,
            serving(bridge, bridge_certs) as stub,
        ):
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await stub.ListLights(lighting_service_pb2.ListLightsRequest())
            assert raised.value.code() == grpc.StatusCode.UNAVAILABLE
            reads = len(bridge.requests)

            with pytest.raises(grpc.aio.AioRpcError):
                await stub.UpdateLight(
                    lighting_service_pb2.UpdateLightRequest(
                        light_id=LIGHT_ID, command=command
                    )
                )
            assert len(bridge.requests) - reads == 1

        assert reads > 1

    run(scenario())


# Before anything has paired


def test_an_unpaired_gateway_says_what_is_missing() -> None:
    async def scenario() -> None:
        async with gateway_for(None) as stub:
            calls = (
                stub.ListLights(lighting_service_pb2.ListLightsRequest()),
                stub.GetLight(lighting_service_pb2.GetLightRequest(light_id=LIGHT_ID)),
                stub.UpdateLight(
                    lighting_service_pb2.UpdateLightRequest(light_id=LIGHT_ID)
                ),
            )
            for call in calls:
                with pytest.raises(grpc.aio.AioRpcError) as raised:
                    await call
                assert raised.value.code() == grpc.StatusCode.FAILED_PRECONDITION
                assert "pair" in raised.value.details()

    run(scenario())


def test_the_service_announces_itself(bridge_certs: BridgeCerts) -> None:
    """Reflection is how a client that knows nothing finds the RPCs."""

    async def scenario() -> None:
        async with (
            running_gateway(EPHEMERAL, [hosted_lighting_service(None)]) as gw,
            grpc.aio.insecure_channel(f"127.0.0.1:{gw.port}") as channel,
        ):
            stub = reflection_pb2_grpc.ServerReflectionStub(channel)

            async def requests() -> AsyncIterator[
                reflection_pb2.ServerReflectionRequest
            ]:
                yield reflection_pb2.ServerReflectionRequest(list_services="*")

            call = stub.ServerReflectionInfo(requests())
            response = await call.read()
            call.cancel()

        listed = {service.name for service in response.list_services_response.service}
        assert SERVICE_NAME in listed

    run(scenario())
