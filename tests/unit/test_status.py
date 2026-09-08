"""The gRPC status a Gateway failure reaches a client as.

Two claims per failure, and they are different claims: which code a client
branches on, and whether what the Bridge said about it survived the trip.
"""

from __future__ import annotations

from typing import Any

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
from hue_grpc.status import status_for


def code_for(failure: Exception) -> grpc.StatusCode:
    return status_for(failure).code


def test_an_unpressed_link_button_is_recoverable_rather_than_a_fault() -> None:
    """The caller can fix this by walking to the Bridge, so say so precisely."""
    unpressed = LinkButtonNotPressedError(101, "link button not pressed")

    answer = status_for(unpressed)

    assert answer.code == grpc.StatusCode.FAILED_PRECONDITION
    # "Press the button" and "your bridge is offline" must not arrive as the
    # same code.
    assert answer.code != grpc.StatusCode.UNAVAILABLE
    assert answer.code != grpc.StatusCode.INTERNAL
    assert "link button not pressed" in answer.message


def test_any_other_pairing_failure_is_an_unexpected_one() -> None:
    for failure in (
        PairingRejectedError(7, "invalid value, devicetype"),
        MalformedPairingResponseError("no success and no error"),
    ):
        assert code_for(failure) == grpc.StatusCode.INTERNAL


def test_bridge_failures_are_told_apart() -> None:
    """Wait, fix your request, or call someone: three different answers."""
    assert code_for(BridgeUnreachableError("no route")) == grpc.StatusCode.UNAVAILABLE
    assert (
        code_for(BridgeTimeoutError("read timed out"))
        == grpc.StatusCode.DEADLINE_EXCEEDED
    )
    assert (
        code_for(MalformedResponseError("html, not json")) == grpc.StatusCode.INTERNAL
    )


def test_a_rejected_application_key_is_not_the_client_s_token() -> None:
    """The caller's Gateway Token is fine; it is ours the Bridge refused."""
    refused = BridgeResponseError(403, {"errors": [{"description": "unauthorized"}]})

    answer = status_for(refused)

    assert answer.code == grpc.StatusCode.FAILED_PRECONDITION
    assert answer.code != grpc.StatusCode.UNAUTHENTICATED


def test_the_bridge_s_own_http_failures_keep_their_meaning() -> None:
    for status_code, expected in (
        (400, grpc.StatusCode.INVALID_ARGUMENT),
        (404, grpc.StatusCode.NOT_FOUND),
        (405, grpc.StatusCode.UNIMPLEMENTED),
        (429, grpc.StatusCode.RESOURCE_EXHAUSTED),
        (501, grpc.StatusCode.UNIMPLEMENTED),
        (503, grpc.StatusCode.UNAVAILABLE),
        (418, grpc.StatusCode.INTERNAL),
    ):
        assert code_for(BridgeResponseError(status_code, None)) == expected


def test_a_request_refused_before_it_was_sent_is_the_client_s_to_fix() -> None:
    assert (
        code_for(InvalidLightIdError("../bridge")) == grpc.StatusCode.INVALID_ARGUMENT
    )
    assert (
        code_for(InvalidCommandError("brightness is 101"))
        == grpc.StatusCode.INVALID_ARGUMENT
    )
    assert code_for(LightNotFoundError("no such light")) == grpc.StatusCode.NOT_FOUND


# What the Bridge said. A status code says which kind of failure it was; only
# the message can say which field Hue objected to.


def test_what_the_bridge_objected_to_survives_becoming_a_status() -> None:
    refused = BridgeResponseError(
        400,
        {
            "errors": [
                {"description": "invalid value, dimming.brightness, 101"},
                {"description": "device (light) has communication issues"},
            ]
        },
    )

    message = status_for(refused).message

    assert "HTTP 400" in message
    assert "invalid value, dimming.brightness, 101" in message
    assert "device (light) has communication issues" in message


def test_a_bridge_that_failed_without_words_still_says_what_it_answered() -> None:
    wordless: tuple[Any, ...] = (None, "<html>502 Bad Gateway</html>", {"errors": []})

    for payload in wordless:
        assert status_for(BridgeResponseError(502, payload)).message == (
            "bridge returned HTTP 502"
        )


def test_a_failure_the_bridge_had_no_part_in_speaks_for_itself() -> None:
    refused = InvalidLightIdError("'../bridge' is not a resource id")

    assert status_for(refused).message == "'../bridge' is not a resource id"
