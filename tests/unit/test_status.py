"""The gRPC status a Gateway failure reaches a client as."""

from __future__ import annotations

import grpc

from hue_grpc.codec import InvalidCommandError
from hue_grpc.hue.lights import InvalidLightIdError, LightNotFoundError
from hue_grpc.hue.pairing import (
    LinkButtonNotPressedError,
    MalformedPairingResponseError,
    PairingRejectedError,
)
from hue_grpc.hue.transport import (
    BridgeResponseError,
    BridgeTimeoutError,
    BridgeUnreachableError,
    MalformedResponseError,
)
from hue_grpc.status import grpc_status_for


def test_an_unpressed_link_button_is_recoverable_rather_than_a_fault() -> None:
    """The caller can fix this by walking to the Bridge, so say so precisely."""
    unpressed = LinkButtonNotPressedError(101, "link button not pressed")

    status = grpc_status_for(unpressed)

    assert status == grpc.StatusCode.FAILED_PRECONDITION
    # Not the status issue #11 gives an unreachable Bridge: "press the button"
    # and "your bridge is offline" must not arrive as the same code.
    assert status != grpc.StatusCode.UNAVAILABLE
    assert status != grpc.StatusCode.INTERNAL


def test_any_other_pairing_failure_is_an_unexpected_one() -> None:
    for failure in (
        PairingRejectedError(7, "invalid value, devicetype"),
        MalformedPairingResponseError("no success and no error"),
    ):
        assert grpc_status_for(failure) == grpc.StatusCode.INTERNAL


def test_bridge_failures_are_told_apart() -> None:
    """Wait, fix your request, or call someone: three different answers."""
    assert (
        grpc_status_for(BridgeUnreachableError("no route"))
        == grpc.StatusCode.UNAVAILABLE
    )
    assert (
        grpc_status_for(BridgeTimeoutError("read timed out"))
        == grpc.StatusCode.DEADLINE_EXCEEDED
    )
    assert (
        grpc_status_for(MalformedResponseError("html, not json"))
        == grpc.StatusCode.INTERNAL
    )


def test_a_rejected_application_key_is_not_the_client_s_token() -> None:
    """The caller's Gateway Token is fine; it is ours the Bridge refused."""
    refused = BridgeResponseError(403, {"errors": [{"description": "unauthorized"}]})

    status = grpc_status_for(refused)

    assert status == grpc.StatusCode.FAILED_PRECONDITION
    assert status != grpc.StatusCode.UNAUTHENTICATED


def test_the_bridge_s_own_http_failures_keep_their_meaning() -> None:
    for status_code, expected in (
        (400, grpc.StatusCode.INVALID_ARGUMENT),
        (404, grpc.StatusCode.NOT_FOUND),
        (429, grpc.StatusCode.RESOURCE_EXHAUSTED),
        (503, grpc.StatusCode.UNAVAILABLE),
        (418, grpc.StatusCode.INTERNAL),
    ):
        assert grpc_status_for(BridgeResponseError(status_code, None)) == expected


def test_a_request_refused_before_it_was_sent_is_the_client_s_to_fix() -> None:
    assert (
        grpc_status_for(InvalidLightIdError("../bridge"))
        == grpc.StatusCode.INVALID_ARGUMENT
    )
    assert (
        grpc_status_for(InvalidCommandError("brightness is 101"))
        == grpc.StatusCode.INVALID_ARGUMENT
    )
    assert (
        grpc_status_for(LightNotFoundError("no such light"))
        == grpc.StatusCode.NOT_FOUND
    )
