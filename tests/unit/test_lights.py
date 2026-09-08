"""The Bridge calls behind the three lighting RPCs.

Each one goes through the real transport to a TLS listener presenting a
Bridge-shaped certificate, so the path, the headers and the body are the ones
a Bridge would actually receive.
"""

from __future__ import annotations

import json
import logging

import pytest
from conftest import BRIDGE_ID, BridgeCerts, FakeBridge, run

from hue_grpc.hue.lights import (
    LIGHT_COLLECTION,
    InvalidLightIdError,
    LightNotFoundError,
    Lights,
)
from hue_grpc.hue.transport import HueTransport, MalformedResponseError

LIGHT_ID = "8f2a1e00-0000-4000-8000-000000000001"

ONE_LIGHT = json.dumps(
    {"errors": [], "data": [{"type": "light", "id": LIGHT_ID, "on": {"on": True}}]}
)


def lights_on(bridge: FakeBridge, certs: BridgeCerts) -> Lights:
    return Lights(
        HueTransport(
            bridge_id=BRIDGE_ID,
            address=bridge.address,
            application_key="an-application-key",
            ca_pem=certs.ca_pem,
        )
    )


def test_lists_the_light_collection(bridge_certs: BridgeCerts) -> None:
    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body=ONE_LIGHT) as bridge:
            lights = lights_on(bridge, bridge_certs)
            async with lights.transport:
                found = await lights.all()

        assert [light["id"] for light in found] == [LIGHT_ID]
        assert (
            bridge.requests[0].decode().startswith(f"GET {LIGHT_COLLECTION} HTTP/1.1")
        )

    run(scenario())


def test_reads_one_light_by_id(bridge_certs: BridgeCerts) -> None:
    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body=ONE_LIGHT) as bridge:
            lights = lights_on(bridge, bridge_certs)
            async with lights.transport:
                light = await lights.one(LIGHT_ID)

        assert light["id"] == LIGHT_ID
        assert (
            bridge.requests[0]
            .decode()
            .startswith(f"GET {LIGHT_COLLECTION}/{LIGHT_ID} HTTP/1.1")
        )

    run(scenario())


def test_a_light_the_bridge_does_not_have_is_not_found(
    bridge_certs: BridgeCerts,
) -> None:
    """Hue answers 404 with its own error envelope; the id is what was wrong."""
    body = json.dumps({"errors": [{"description": "no such resource"}]})

    async def scenario() -> None:
        async with FakeBridge(
            bridge_certs, body=body, status="404 Not Found"
        ) as bridge:
            lights = lights_on(bridge, bridge_certs)
            async with lights.transport:
                with pytest.raises(LightNotFoundError):
                    await lights.one(LIGHT_ID)

    run(scenario())


def test_an_empty_collection_for_one_light_is_not_found(
    bridge_certs: BridgeCerts,
) -> None:
    async def scenario() -> None:
        body = json.dumps({"errors": [], "data": []})
        async with FakeBridge(bridge_certs, body=body) as bridge:
            lights = lights_on(bridge, bridge_certs)
            async with lights.transport:
                with pytest.raises(LightNotFoundError):
                    await lights.one(LIGHT_ID)

    run(scenario())


def test_an_answer_without_data_is_malformed(bridge_certs: BridgeCerts) -> None:
    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body='{"errors": []}') as bridge:
            lights = lights_on(bridge, bridge_certs)
            async with lights.transport:
                with pytest.raises(MalformedResponseError):
                    await lights.all()

    run(scenario())


def test_changing_a_light_puts_the_command_and_reports_what_changed(
    bridge_certs: BridgeCerts,
) -> None:
    body = json.dumps({"errors": [], "data": [{"rid": LIGHT_ID, "rtype": "light"}]})

    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body=body) as bridge:
            lights = lights_on(bridge, bridge_certs)
            async with lights.transport:
                changed = await lights.change(LIGHT_ID, {"on": {"on": False}})

        assert [resource["rid"] for resource in changed.updated] == [LIGHT_ID]
        assert changed.errors == []
        request = bridge.requests[0].decode()
        assert request.startswith(f"PUT {LIGHT_COLLECTION}/{LIGHT_ID} HTTP/1.1")
        assert json.loads(request.split("\r\n\r\n", 1)[1]) == {"on": {"on": False}}

    run(scenario())


def test_a_partly_refused_change_carries_both_halves(
    bridge_certs: BridgeCerts,
) -> None:
    """Hue reports what it did and what it would not do, in one 200 answer."""
    body = json.dumps(
        {
            "errors": [{"description": "device (light) has communication issues"}],
            "data": [{"rid": LIGHT_ID, "rtype": "light"}],
        }
    )

    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body=body) as bridge:
            lights = lights_on(bridge, bridge_certs)
            async with lights.transport:
                changed = await lights.change(LIGHT_ID, {"on": {"on": True}})

        assert len(changed.updated) == 1
        assert changed.errors[0]["description"].startswith("device (light) has")

    run(scenario())


@pytest.mark.parametrize(
    "light_id",
    ["", "   ", "../bridge", "abc/def", "abc def", "a" * 65, "abc?query", "a.b"],
)
def test_an_id_that_could_change_the_path_never_reaches_the_bridge(
    bridge_certs: BridgeCerts, light_id: str
) -> None:
    """The id lands in a URL path. Anything that could steer it is refused."""

    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body=ONE_LIGHT) as bridge:
            lights = lights_on(bridge, bridge_certs)
            async with lights.transport:
                with pytest.raises(InvalidLightIdError):
                    await lights.one(light_id)
                with pytest.raises(InvalidLightIdError):
                    await lights.change(light_id, {"on": {"on": True}})

        assert bridge.requests == []

    run(scenario())


def test_an_error_the_bridge_attached_to_a_read_is_not_swallowed(
    bridge_certs: BridgeCerts, caplog: pytest.LogCaptureFixture
) -> None:
    """A read has nowhere on the wire to put one, which is not a reason to
    behave as though the bridge never said it."""
    body = json.dumps(
        {
            "errors": [{"description": "device (light) has communication issues"}],
            "data": [{"type": "light", "id": LIGHT_ID, "on": {"on": True}}],
        }
    )

    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body=body) as bridge:
            lights = lights_on(bridge, bridge_certs)
            async with lights.transport:
                assert [light["id"] for light in await lights.all()] == [LIGHT_ID]

    with caplog.at_level(logging.WARNING):
        run(scenario())

    assert "device (light) has communication issues" in caplog.text
    assert LIGHT_COLLECTION in caplog.text
