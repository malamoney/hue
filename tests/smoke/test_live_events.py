"""Opt-in event-stream checks against a real Bridge.

Not part of the packaged test run: it needs hardware and an Application Key::

    HUE_BRIDGE_ADDRESS=192.168.86.223 HUE_BRIDGE_ID=ECB5FAFFFE334703 \
        pytest tests/smoke -k events

Nothing here waits for a light to change, because nothing here can make one
change: what it proves is the half that hardware is needed for — that the
Bridge accepts the Application Key on `/eventstream/clip/v2`, serves
`text/event-stream` there, and holds the connection open through a silence
that would be a timeout on any other path. The rest — parsing, fanning out,
the Gap on reconnect, the Resync — is `tests/unit` and needs no Bridge.

The second test asks the Bridge to be quiet for eight seconds, which is the
only way to tell "held open" from "answered and closed" without waiting for
somebody to touch a switch.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from hue_grpc.hue.events import BridgeEvents
from hue_grpc.hue.transport import HueTransport
from hue_grpc.registry import Registry, default_registry_path

ADDRESS = os.environ.get("HUE_BRIDGE_ADDRESS", "")
BRIDGE_ID = os.environ.get("HUE_BRIDGE_ID", "")


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


def events() -> BridgeEvents:
    return BridgeEvents(
        HueTransport(bridge_id=BRIDGE_ID, address=ADDRESS, application_key=KEY)
    )


def test_opens_the_event_stream_on_a_real_bridge() -> None:
    """A wrong key or a wrong path fails here, and only here."""

    async def scenario() -> None:
        stream = events()
        async with stream.transport, stream.connected():
            pass

    asyncio.run(scenario())


def test_holds_the_stream_open_through_a_silence() -> None:
    """Eight seconds of nothing, which the read timeout would otherwise end."""

    async def scenario() -> None:
        stream = events()
        async with stream.transport, stream.connected() as batches:

            async def read() -> None:
                async for _ in batches:
                    pass

            # Running out of batches is the failure: it means the Bridge
            # closed the stream. Whether anything happened on it in those
            # eight seconds is not ours to arrange.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(read(), 8)

    asyncio.run(scenario())
