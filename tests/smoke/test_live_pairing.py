"""Opt-in Pairing checks against a real Bridge. Needs hardware and a hand.

Not part of the packaged test run. The unpressed-button half runs with the
same environment as the rest of the smoke suite::

    HUE_BRIDGE_ADDRESS=192.168.86.223 HUE_BRIDGE_ID=ECB5FAFFFE334703 \
        pytest tests/smoke

The other half mints a real Application Key, so it is gated separately. Press
the Bridge's link button, then within thirty seconds run::

    HUE_BRIDGE_ADDRESS=... HUE_BRIDGE_ID=... HUE_PRESS_LINK_BUTTON=1 \
        pytest tests/smoke -k mints

It leaves an entry named `hue-grpc#smoke-test` on the Bridge; nothing here
persists the secrets, so delete that entry from the Hue app when done.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from hue_grpc.hue.pairing import LinkButtonNotPressedError, pair
from hue_grpc.hue.transport import HueTransport

ADDRESS = os.environ.get("HUE_BRIDGE_ADDRESS", "")
BRIDGE_ID = os.environ.get("HUE_BRIDGE_ID", "")
# The operator promising to have pressed the button, not a fact
# anything here can check.
BUTTON_PRESS_PROMISED = os.environ.get("HUE_PRESS_LINK_BUTTON") == "1"

pytestmark = pytest.mark.skipif(
    not (ADDRESS and BRIDGE_ID),
    reason="set HUE_BRIDGE_ADDRESS and HUE_BRIDGE_ID to run against a Bridge",
)


@pytest.mark.skipif(
    BUTTON_PRESS_PROMISED, reason="the link button was just pressed on purpose"
)
def test_reports_an_unpressed_link_button_rather_than_failing() -> None:
    async def scenario() -> None:
        async with HueTransport(bridge_id=BRIDGE_ID, address=ADDRESS) as transport:
            with pytest.raises(LinkButtonNotPressedError) as refusal:
                await pair(transport, instance="smoke-test")

            assert "link button" in str(refusal.value)
            assert transport.application_key is None

    asyncio.run(scenario())


@pytest.mark.skipif(
    not BUTTON_PRESS_PROMISED,
    reason="set HUE_PRESS_LINK_BUTTON=1 after pressing the button",
)
def test_mints_both_secrets_from_a_pressed_link_button() -> None:
    async def scenario() -> None:
        async with HueTransport(bridge_id=BRIDGE_ID, address=ADDRESS) as transport:
            secrets = await pair(transport, instance="smoke-test")

            assert secrets.application_key
            assert secrets.client_key
            # The proof that the minted key is the real thing: a CLIP v2 call,
            # which the same transport could not have made a moment ago.
            bridge = await transport.request("GET", "/clip/v2/resource/bridge")

        assert bridge["data"][0]["bridge_id"].casefold() == BRIDGE_ID.casefold()

    asyncio.run(scenario())
