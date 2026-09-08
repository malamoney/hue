"""Opt-in lighting checks against a real Bridge, through the real RPCs.

Not part of the packaged test run: it needs hardware, and an Application Key
that only a button press can mint. Reading needs the key and nothing else::

    HUE_BRIDGE_ADDRESS=192.168.86.223 HUE_BRIDGE_ID=ECB5FAFFFE334703 \
        pytest tests/smoke -k lights

The key is taken from `HUE_APPLICATION_KEY`, or from the Registry if
`hue-grpc-server pair` has already written one there.

The last test changes a light, so it is gated separately::

    HUE_CHANGE_LIGHTS=1 pytest tests/smoke -k leaves_the_power_alone

It sets a light's brightness to the brightness it already has, which is the
smallest change that still proves the write path, and then re-reads it to
show that the fields the command left out came through untouched. Nothing
visible should happen.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import grpc
import pytest

from hue.v1 import lighting_pb2, lighting_service_pb2
from hue.v1 import lighting_service_pb2_grpc as lighting_grpc
from hue_grpc.hue.lights import Lights
from hue_grpc.hue.transport import HueTransport
from hue_grpc.lighting.service import hosted_lighting_service
from hue_grpc.registry import Registry, default_registry_path
from hue_grpc.serving.config import GatewayConfig
from hue_grpc.serving.serve import running_gateway

ADDRESS = os.environ.get("HUE_BRIDGE_ADDRESS", "")
BRIDGE_ID = os.environ.get("HUE_BRIDGE_ID", "")
# The operator agreeing that the lights may be written to, not something
# anything here can infer.
CHANGES_ALLOWED = os.environ.get("HUE_CHANGE_LIGHTS") == "1"


def application_key() -> str:
    """The key from the environment, or the one Pairing wrote down."""
    from_environment = os.environ.get("HUE_APPLICATION_KEY", "")
    if from_environment:
        return from_environment
    entry = Registry(default_registry_path()).load()
    return "" if entry is None else entry.application_key


KEY = application_key()

pytestmark = pytest.mark.skipif(
    not (ADDRESS and BRIDGE_ID and KEY),
    reason="set HUE_BRIDGE_ADDRESS, HUE_BRIDGE_ID and an application key",
)


@asynccontextmanager
async def gateway() -> AsyncIterator[lighting_grpc.LightingServiceStub]:
    """The whole stack over a real Bridge: listener, servicer, transport."""
    transport = HueTransport(bridge_id=BRIDGE_ID, address=ADDRESS, application_key=KEY)
    config = GatewayConfig(port=0, shutdown_drain=0)
    async with (
        transport,
        running_gateway(
            config, [hosted_lighting_service(Lights(transport))]
        ) as running,
        grpc.aio.insecure_channel(f"127.0.0.1:{running.port}") as channel,
    ):
        yield lighting_grpc.LightingServiceStub(channel)


def test_lists_the_lights_on_a_real_bridge() -> None:
    async def scenario() -> None:
        async with gateway() as stub:
            response = await stub.ListLights(lighting_service_pb2.ListLightsRequest())

        assert response.lights, "the bridge reported no lights at all"
        for light in response.lights:
            assert light.id
            assert light.type == "light"
            # Every light has power. Anything else it has depends on what it
            # is, which is the point of reading capabilities rather than
            # assuming them.
            assert light.HasField("on")

    asyncio.run(scenario())


def test_reads_one_real_light_by_id() -> None:
    async def scenario() -> None:
        async with gateway() as stub:
            listed = await stub.ListLights(lighting_service_pb2.ListLightsRequest())
            first = listed.lights[0]

            light = await stub.GetLight(
                lighting_service_pb2.GetLightRequest(light_id=first.id)
            )

        assert light.id == first.id
        assert light.metadata.name == first.metadata.name

    asyncio.run(scenario())


@pytest.mark.skipif(
    not CHANGES_ALLOWED, reason="set HUE_CHANGE_LIGHTS=1 to write to real lights"
)
def test_setting_brightness_leaves_the_power_alone() -> None:
    """Issue #10's acceptance, against hardware: omission changes nothing."""

    async def scenario() -> None:
        async with gateway() as stub:
            listed = await stub.ListLights(lighting_service_pb2.ListLightsRequest())
            dimmable = [light for light in listed.lights if light.HasField("dimming")]
            if not dimmable:
                pytest.skip("no dimmable light on this bridge")
            before = dimmable[0]

            command = lighting_pb2.LightPut()
            # The brightness it already has: the write path, exercised, with
            # nothing to see.
            command.dimming.brightness = before.dimming.brightness
            changed = await stub.UpdateLight(
                lighting_service_pb2.UpdateLightRequest(
                    light_id=before.id, command=command
                )
            )
            assert not changed.errors
            assert [resource.rid for resource in changed.updated] == [before.id]

            after = await stub.GetLight(
                lighting_service_pb2.GetLightRequest(light_id=before.id)
            )

        assert after.on.on == before.on.on
        assert after.dimming.brightness == pytest.approx(
            before.dimming.brightness, abs=1.0
        )

    asyncio.run(scenario())
