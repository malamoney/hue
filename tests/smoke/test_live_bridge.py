"""Opt-in smoke test against a real Bridge on the local network.

Not part of the packaged test run: it needs hardware. Point it at a Bridge and
run it by hand::

    HUE_BRIDGE_ADDRESS=192.168.86.223 HUE_BRIDGE_ID=ECB5FAFFFE334703 \
        pytest tests/smoke

`GET /api/config` is used because it needs no Application Key, so the test
proves the TLS path alone: Philips' vendored CA verifies the chain, and the
Bridge's certificate names the Bridge we meant to reach.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from hue_grpc.hue.tls import BridgeIdentityError
from hue_grpc.hue.transport import HueTransport

ADDRESS = os.environ.get("HUE_BRIDGE_ADDRESS", "")
BRIDGE_ID = os.environ.get("HUE_BRIDGE_ID", "")

pytestmark = pytest.mark.skipif(
    not (ADDRESS and BRIDGE_ID),
    reason="set HUE_BRIDGE_ADDRESS and HUE_BRIDGE_ID to run against a Bridge",
)


def test_reads_the_bridge_config_over_a_verified_connection() -> None:
    async def scenario() -> None:
        async with HueTransport(bridge_id=BRIDGE_ID, address=ADDRESS) as transport:
            config = await transport.request("GET", "/api/config")

        assert config["bridgeid"].casefold() == BRIDGE_ID.casefold()

    asyncio.run(scenario())


def test_refuses_the_bridge_when_a_different_bridge_is_expected() -> None:
    async def scenario() -> None:
        async with HueTransport(
            bridge_id="ECB5FAFFFE000000", address=ADDRESS
        ) as transport:
            with pytest.raises(BridgeIdentityError):
                await transport.request("GET", "/api/config")

    asyncio.run(scenario())
