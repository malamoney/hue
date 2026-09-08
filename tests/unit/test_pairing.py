"""Pairing: minting an Application Key from a Bridge whose button was pressed.

Every test drives the real transport against a TLS listener presenting a
Bridge-shaped certificate, because Pairing is the one exchange where the
brand-new secret crosses the wire — proving it through the verified
connection is the point, not an incidental detail.
"""

from __future__ import annotations

import json
import logging

import pytest
from conftest import BRIDGE_ID, BridgeCerts, FakeBridge, run

from hue_grpc.hue.pairing import (
    LINK_BUTTON_NOT_PRESSED,
    LinkButtonNotPressedError,
    MalformedPairingResponseError,
    PairedSecrets,
    PairingRejectedError,
    device_type,
    pair,
)
from hue_grpc.hue.tls import BridgeIdentityError
from hue_grpc.hue.transport import APPLICATION_KEY_HEADER, HueTransport

MINTED = (
    '[{"success": {"username": "an-application-key", "clientkey": "a-client-key"}}]'
)
# The refusal in the two shapes it is written down in: the list of one a
# Bridge sends, and the bare object issue #7 quotes.
UNPRESSED_ENTRY = (
    '{"error": {"type": 101, "address": "/", "description": "link button not pressed"}}'
)
UNPRESSED = f"[{UNPRESSED_ENTRY}]"


def transport_to(bridge: FakeBridge, certs: BridgeCerts) -> HueTransport:
    return HueTransport(
        bridge_id=BRIDGE_ID, address=bridge.address, ca_pem=certs.ca_pem
    )


def test_mints_both_secrets_when_the_link_button_was_pressed(
    bridge_certs: BridgeCerts,
) -> None:
    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body=MINTED) as bridge:
            transport = transport_to(bridge, bridge_certs)
            async with transport:
                secrets = await pair(transport, instance="gateway")

            assert secrets == PairedSecrets(
                application_key="an-application-key", client_key="a-client-key"
            )
            head, _, body = bridge.requests[0].partition(b"\r\n\r\n")
            assert head.startswith(b"POST /api HTTP/1.1")
            # The v1 endpoint predates the header, and there is no key yet.
            assert APPLICATION_KEY_HEADER.encode() not in head.lower()
            # generateclientkey is asked for even though nothing uses the
            # Client Key yet: obtaining it later means another button press.
            assert json.loads(body) == {
                "devicetype": "hue-grpc#gateway",
                "generateclientkey": True,
            }

    run(scenario())


def test_sends_the_minted_application_key_on_every_later_request(
    bridge_certs: BridgeCerts,
) -> None:
    """Pairing leaves the transport authenticated; the caller need not wire it."""

    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body=MINTED) as bridge:
            transport = transport_to(bridge, bridge_certs)
            async with transport:
                assert transport.application_key is None
                await pair(transport, instance="gateway")
                assert transport.application_key == "an-application-key"
                await transport.request("GET", "/clip/v2/resource/light")

            later = bridge.requests[1].decode()
            assert f"{APPLICATION_KEY_HEADER}: an-application-key" in later

    run(scenario())


def test_reports_an_unpressed_link_button_as_a_recoverable_outcome(
    bridge_certs: BridgeCerts,
) -> None:
    """Type 101 is the user not having walked to the Bridge yet, not a fault."""

    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body=UNPRESSED) as bridge:
            transport = transport_to(bridge, bridge_certs)
            async with transport:
                with pytest.raises(LinkButtonNotPressedError) as refusal:
                    await pair(transport, instance="gateway")

            assert refusal.value.hue_error_type == LINK_BUTTON_NOT_PRESSED
            assert "link button" in str(refusal.value)
            # Nothing was minted, so nothing may be sent as though it had been.
            assert transport.application_key is None

    run(scenario())


def test_reads_the_refusal_when_it_arrives_unwrapped(
    bridge_certs: BridgeCerts,
) -> None:
    """The same answer, sent as the bare object rather than a list of one."""

    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body=UNPRESSED_ENTRY) as bridge:
            transport = transport_to(bridge, bridge_certs)
            async with transport:
                with pytest.raises(LinkButtonNotPressedError):
                    await pair(transport, instance="gateway")

    run(scenario())


def test_reports_any_other_refusal_as_a_distinct_failure(
    bridge_certs: BridgeCerts,
) -> None:
    """A Bridge that refuses for another reason must not read as a button press."""
    rejected = (
        '[{"error": {"type": 7, "address": "/devicetype", '
        '"description": "invalid value, devicetype"}}]'
    )

    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body=rejected) as bridge:
            transport = transport_to(bridge, bridge_certs)
            async with transport:
                with pytest.raises(PairingRejectedError) as refusal:
                    await pair(transport, instance="gateway")

            assert not isinstance(refusal.value, LinkButtonNotPressedError)
            assert refusal.value.hue_error_type == 7
            assert "invalid value, devicetype" in str(refusal.value)

    run(scenario())


def test_accepts_a_bridge_that_mints_no_client_key(
    bridge_certs: BridgeCerts, caplog: pytest.LogCaptureFixture
) -> None:
    """Older firmware ignores generateclientkey; the Application Key still works."""
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        async with FakeBridge(
            bridge_certs, body='[{"success": {"username": "an-application-key"}}]'
        ) as bridge:
            transport = transport_to(bridge, bridge_certs)
            async with transport:
                secrets = await pair(transport, instance="gateway")

            assert secrets.application_key == "an-application-key"
            assert secrets.client_key is None

    run(scenario())

    # Silence here would strand Entertainment behind another button press with
    # nothing on the record to explain why.
    assert "client key" in caplog.text.casefold()


@pytest.mark.parametrize(
    "body",
    [
        "[]",
        '{"data": []}',
        '[{"success": {"clientkey": "a-client-key"}}]',
        '[{"success": {"username": ""}}]',
        '[{"success": {"username": 42}}]',
        '[{"error": {"description": "no type at all"}}]',
    ],
)
def test_reports_a_response_that_is_neither_a_success_nor_a_refusal(
    bridge_certs: BridgeCerts, body: str
) -> None:
    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body=body) as bridge:
            transport = transport_to(bridge, bridge_certs)
            async with transport:
                with pytest.raises(MalformedPairingResponseError):
                    await pair(transport, instance="gateway")

    run(scenario())


def test_never_pairs_with_a_bridge_that_fails_the_identity_check(
    bridge_certs: BridgeCerts,
) -> None:
    """The mint is when the secret first exists; an unverified peer never sees it."""

    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body=MINTED) as bridge:
            transport = HueTransport(
                bridge_id="ECB5FAFFFE000000",
                address=bridge.address,
                ca_pem=bridge_certs.ca_pem,
            )
            async with transport:
                with pytest.raises(BridgeIdentityError):
                    await pair(transport, instance="gateway")

            assert bridge.requests == []
            assert transport.application_key is None

    run(scenario())


def test_keeps_the_minted_secrets_out_of_the_log(
    bridge_certs: BridgeCerts, caplog: pytest.LogCaptureFixture
) -> None:
    """Debug logging is on during Pairing precisely when the secrets are new."""
    caplog.set_level(logging.DEBUG)

    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body=MINTED) as bridge:
            transport = transport_to(bridge, bridge_certs)
            async with transport:
                await pair(transport, instance="gateway")
                await transport.request("GET", "/clip/v2/resource/light")

    run(scenario())

    assert "an-application-key" not in caplog.text
    assert "a-client-key" not in caplog.text
    # Still traceable: that Pairing happened, and under which name.
    assert "hue-grpc#gateway" in caplog.text


def test_keeps_the_secrets_out_of_the_repr_that_a_traceback_would_print() -> None:
    secrets = PairedSecrets(
        application_key="an-application-key", client_key="a-client-key"
    )

    assert "an-application-key" not in repr(secrets)
    assert "a-client-key" not in repr(secrets)
    assert "PairedSecrets" in repr(secrets)


@pytest.mark.parametrize(
    "instance",
    [
        "",
        "   ",
        "a-name#with-a-separator",
        # One character past the v1 API's nineteen for the device half.
        "x" * 20,
    ],
)
def test_refuses_an_instance_name_the_bridge_would_reject(instance: str) -> None:
    with pytest.raises(ValueError):
        device_type(instance)


def test_names_the_longest_instance_the_bridge_will_accept() -> None:
    assert device_type("x" * 19) == f"hue-grpc#{'x' * 19}"
