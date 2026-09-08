"""The HTTP transport that carries CLIP v2 and v1 calls to the Bridge.

Every test here talks to a real TLS listener presenting a Bridge-shaped
certificate, so the transport is exercised through its own verified
connection rather than around it.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from conftest import BRIDGE_ID, BridgeCerts, FakeBridge, run

from hue_grpc.hue.tls import BridgeIdentityError
from hue_grpc.hue.transport import (
    APPLICATION_KEY_HEADER,
    BridgeResponseError,
    BridgeTimeoutError,
    BridgeUnreachableError,
    HueTransport,
    MalformedResponseError,
    Timeouts,
)
from hue_grpc.logs import tracking_call


def test_applies_the_application_key_to_clip_v2_requests(
    bridge_certs: BridgeCerts,
) -> None:
    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body='{"data": [{"id": "abc"}]}') as bridge:
            transport = HueTransport(
                bridge_id=BRIDGE_ID,
                address=bridge.address,
                application_key="an-application-key",
                ca_pem=bridge_certs.ca_pem,
            )
            async with transport:
                payload = await transport.request("GET", "/clip/v2/resource/light")

            assert payload == {"data": [{"id": "abc"}]}
            head = bridge.requests[0].decode()
            assert head.startswith("GET /clip/v2/resource/light HTTP/1.1")
            assert "hue-application-key: an-application-key" in head

    run(scenario())


def test_pairs_over_the_v1_api_without_an_application_key(
    bridge_certs: BridgeCerts,
) -> None:
    """Pairing is the one call that predates the Application Key it mints."""
    minted = '[{"success": {"username": "minted-key", "clientkey": "abc"}}]'

    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body=minted) as bridge:
            transport = HueTransport(
                bridge_id=BRIDGE_ID,
                address=bridge.address,
                ca_pem=bridge_certs.ca_pem,
            )
            async with transport:
                payload = await transport.request(
                    "POST",
                    "/api",
                    json={"devicetype": "hue-grpc#gateway", "generateclientkey": True},
                )

            assert payload == [
                {"success": {"username": "minted-key", "clientkey": "abc"}}
            ]
            head, _, body = bridge.requests[0].partition(b"\r\n\r\n")
            assert head.startswith(b"POST /api HTTP/1.1")
            assert APPLICATION_KEY_HEADER.encode() not in head.lower()
            assert json.loads(body) == {
                "devicetype": "hue-grpc#gateway",
                "generateclientkey": True,
            }

    run(scenario())


def test_reports_an_unsuccessful_response_with_its_payload(
    bridge_certs: BridgeCerts,
) -> None:
    error = '{"errors": [{"description": "unauthorized user"}]}'

    async def scenario() -> None:
        async with FakeBridge(
            bridge_certs, body=error, status="401 Unauthorized"
        ) as bridge:
            transport = HueTransport(
                bridge_id=BRIDGE_ID,
                address=bridge.address,
                application_key="an-application-key",
                ca_pem=bridge_certs.ca_pem,
            )
            async with transport:
                with pytest.raises(BridgeResponseError) as failure:
                    await transport.request("GET", "/clip/v2/resource/light")

            assert failure.value.status_code == 401
            assert failure.value.payload == {
                "errors": [{"description": "unauthorized user"}]
            }
            assert "an-application-key" not in str(failure.value)

    run(scenario())


def test_reports_a_body_that_is_not_json(bridge_certs: BridgeCerts) -> None:
    async def scenario() -> None:
        async with FakeBridge(
            bridge_certs, body="<html>bridge is busy</html>", content_type="text/html"
        ) as bridge:
            transport = HueTransport(
                bridge_id=BRIDGE_ID,
                address=bridge.address,
                ca_pem=bridge_certs.ca_pem,
            )
            async with transport:
                with pytest.raises(MalformedResponseError):
                    await transport.request("GET", "/clip/v2/resource/light")

    run(scenario())


def test_refuses_a_bridge_whose_certificate_names_another_bridge(
    bridge_certs: BridgeCerts,
) -> None:
    """The Application Key must never reach a Bridge we have not identified."""

    async def scenario() -> None:
        async with FakeBridge(bridge_certs) as bridge:
            transport = HueTransport(
                bridge_id="ECB5FAFFFE000000",
                address=bridge.address,
                application_key="an-application-key",
                ca_pem=bridge_certs.ca_pem,
            )
            async with transport:
                with pytest.raises(BridgeIdentityError):
                    await transport.request("GET", "/clip/v2/resource/light")

            assert bridge.requests == []

    run(scenario())


def test_gives_up_on_a_bridge_that_accepts_but_never_answers(
    bridge_certs: BridgeCerts,
) -> None:
    """The read timeout is what bounds a wedged Bridge, not the connect timeout."""

    async def stall(writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(2)

    async def scenario() -> None:
        async with FakeBridge(bridge_certs, respond=stall) as bridge:
            transport = HueTransport(
                bridge_id=BRIDGE_ID,
                address=bridge.address,
                ca_pem=bridge_certs.ca_pem,
                timeouts=Timeouts(connect=30.0, read=0.2),
            )
            async with transport:
                with pytest.raises(BridgeTimeoutError):
                    await transport.request("GET", "/clip/v2/resource/light")

    run(scenario())


def test_reports_a_bridge_that_cannot_be_reached() -> None:
    async def scenario() -> None:
        # Port 1 on loopback: nothing listens, and nothing on the network is
        # consulted to find that out.
        transport = HueTransport(bridge_id=BRIDGE_ID, address="127.0.0.1:1")
        async with transport:
            with pytest.raises(BridgeUnreachableError):
                await transport.request("GET", "/clip/v2/resource/light")

    run(scenario())


def test_streams_events_across_gaps_longer_than_the_request_timeout(
    bridge_certs: BridgeCerts,
) -> None:
    """The Bridge is silent between events; that silence is not a timeout."""

    async def dribble(writer: asyncio.StreamWriter) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        await asyncio.sleep(0.3)
        writer.write(b'data: [{"id": "an-event"}]\n\n')
        await writer.drain()
        writer.close()

    async def scenario() -> None:
        async with FakeBridge(bridge_certs, respond=dribble) as bridge:
            transport = HueTransport(
                bridge_id=BRIDGE_ID,
                address=bridge.address,
                application_key="an-application-key",
                ca_pem=bridge_certs.ca_pem,
                timeouts=Timeouts(read=0.1),
            )
            async with (
                transport,
                transport.stream("GET", "/eventstream/clip/v2") as events,
            ):
                assert events.status_code == 200
                payloads = [
                    line
                    async for line in events.aiter_lines()
                    if line.startswith("data: ")
                ]

            assert payloads == ['data: [{"id": "an-event"}]']
            sent = bridge.requests[0].decode()
            assert sent.startswith("GET /eventstream/clip/v2 HTTP/1.1")
            assert "hue-application-key: an-application-key" in sent

    run(scenario())


def test_keeps_the_application_key_out_of_the_log(
    bridge_certs: BridgeCerts, caplog: pytest.LogCaptureFixture
) -> None:
    """Debug logging must stay usable without putting the secret on disk."""
    caplog.set_level(logging.DEBUG)

    async def scenario() -> None:
        async with FakeBridge(bridge_certs) as bridge:
            transport = HueTransport(
                bridge_id=BRIDGE_ID,
                address=bridge.address,
                application_key="an-application-key",
                ca_pem=bridge_certs.ca_pem,
            )
            async with transport:
                await transport.request("GET", "/clip/v2/resource/light")

    run(scenario())

    assert "an-application-key" not in caplog.text
    # Still traceable: the request, and the fact the header was on it.
    assert "/clip/v2/resource/light" in caplog.text
    assert APPLICATION_KEY_HEADER in caplog.text


def test_keeps_the_status_of_a_failure_whose_body_is_not_json(
    bridge_certs: BridgeCerts,
) -> None:
    """A Bridge under load answers 5xx with HTML; callers still need the status."""

    async def scenario() -> None:
        async with FakeBridge(
            bridge_certs,
            body="<html>bridge is busy</html>",
            content_type="text/html",
            status="503 Service Unavailable",
        ) as bridge:
            transport = HueTransport(
                bridge_id=BRIDGE_ID,
                address=bridge.address,
                ca_pem=bridge_certs.ca_pem,
            )
            async with transport:
                with pytest.raises(BridgeResponseError) as failure:
                    await transport.request("GET", "/clip/v2/resource/light")

            assert failure.value.status_code == 503
            assert "bridge is busy" in str(failure.value.payload)

    run(scenario())


def test_accepts_a_successful_response_with_no_body(
    bridge_certs: BridgeCerts,
) -> None:
    """An empty body is an answer, not a malformed one."""

    async def scenario() -> None:
        async with FakeBridge(bridge_certs, body="", status="204 No Content") as bridge:
            transport = HueTransport(
                bridge_id=BRIDGE_ID,
                address=bridge.address,
                application_key="an-application-key",
                ca_pem=bridge_certs.ca_pem,
            )
            async with transport:
                payload = await transport.request(
                    "PUT", "/clip/v2/resource/light/abc", json={"on": {"on": True}}
                )

            assert payload is None

    run(scenario())


def test_reports_the_bridges_status_to_whatever_rpc_is_being_served(
    bridge_certs: BridgeCerts,
) -> None:
    """The Bridge's HTTP status belongs on the RPC's log line, and this is the
    only layer that ever sees it. Outside an RPC there is nothing to tell."""

    async def scenario() -> None:
        async with FakeBridge(bridge_certs, status="207 Multi-Status") as bridge:
            transport = HueTransport(
                bridge_id=BRIDGE_ID,
                address=bridge.address,
                ca_pem=bridge_certs.ca_pem,
            )
            async with transport:
                with tracking_call("a-correlation-id") as call:
                    await transport.request("GET", "/clip/v2/resource/light")

                assert call.upstream_status == 207

    run(scenario())
