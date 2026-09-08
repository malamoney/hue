"""The gRPC status Pairing's failures reach a client as."""

from __future__ import annotations

import grpc

from hue_grpc.hue.pairing import (
    LinkButtonNotPressedError,
    MalformedPairingResponseError,
    PairingRejectedError,
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
