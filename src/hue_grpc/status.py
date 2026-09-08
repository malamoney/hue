"""The gRPC status a Gateway failure surfaces to the client.

One table, in one place, because the distinctions it draws are the ones a
client acts on: press the button, fix the request, wait and try again, or call
someone. Spread across the servicers they would drift apart, and a caller
cannot tell "your bridge is offline" from "your light id is wrong" by reading
a message.

Issue #11 owns the rest of it: Hue reports application errors inside exchanges
it answered with HTTP 200, and those stay in the typed response —
`MutationResponse.errors` — rather than becoming a status at all. What is here
is the other half, where the Bridge could not be reached, would not answer, or
answered with something unusable.

It lives above `hue_grpc.hue`, which talks to the Bridge and knows nothing
about gRPC.
"""

from __future__ import annotations

import grpc

from hue_grpc.codec import InvalidCommandError
from hue_grpc.hue.lights import InvalidLightIdError, LightNotFoundError
from hue_grpc.hue.pairing import LinkButtonNotPressedError
from hue_grpc.hue.transport import (
    BridgeResponseError,
    BridgeTimeoutError,
    BridgeUnreachableError,
)

__all__ = ["grpc_status_for"]


def grpc_status_for(failure: Exception) -> grpc.StatusCode:
    """The status a gRPC client should see for `failure`."""
    if isinstance(failure, LinkButtonNotPressedError):
        # FAILED_PRECONDITION, not UNAVAILABLE: the caller can recover by
        # walking to the Bridge and asking again, and retrying before they do
        # cannot succeed. It also stays legible next to the rest of this
        # table, where UNAVAILABLE already means the Bridge could not be
        # reached — a client must be able to tell "press the button" from
        # "your bridge is offline" without reading the message.
        return grpc.StatusCode.FAILED_PRECONDITION
    if isinstance(failure, InvalidLightIdError | InvalidCommandError):
        # Refused before anything was sent, so nothing changed and the client
        # can fix it and ask again.
        return grpc.StatusCode.INVALID_ARGUMENT
    if isinstance(failure, LightNotFoundError):
        return grpc.StatusCode.NOT_FOUND
    if isinstance(failure, BridgeTimeoutError):
        return grpc.StatusCode.DEADLINE_EXCEEDED
    if isinstance(failure, BridgeUnreachableError):
        return grpc.StatusCode.UNAVAILABLE
    if isinstance(failure, BridgeResponseError):
        return _for_http(failure.status_code)
    # What is left is the Bridge answering with something that is not what it
    # claims to be, or this Gateway failing in a way it did not anticipate.
    # Nothing the client sent caused either and nothing they can send will fix
    # it, which is what INTERNAL is for.
    return grpc.StatusCode.INTERNAL


def _for_http(status_code: int) -> grpc.StatusCode:
    """The status for a Bridge that answered, but not with a success."""
    if status_code in (401, 403):
        # The Bridge no longer accepts our Application Key — revoked from the
        # Hue app, or a factory reset. Not UNAUTHENTICATED: that is about the
        # Gateway Token this client presented, and telling a caller with a
        # perfectly good token that theirs is wrong sends them after the
        # wrong secret. Pairing again is what fixes this one.
        return grpc.StatusCode.FAILED_PRECONDITION
    if status_code == 404:
        return grpc.StatusCode.NOT_FOUND
    if status_code == 400:
        return grpc.StatusCode.INVALID_ARGUMENT
    if status_code == 429:
        return grpc.StatusCode.RESOURCE_EXHAUSTED
    if status_code >= 500:
        # The Bridge is up enough to answer and not well enough to serve;
        # a client retrying with backoff is the right response.
        return grpc.StatusCode.UNAVAILABLE
    return grpc.StatusCode.INTERNAL
