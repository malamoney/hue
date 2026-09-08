"""The gRPC status a Gateway failure surfaces to the client.

One table, in one place, because the distinctions it draws are the ones a
client acts on: press the button, fix the request, wait and try again, or call
someone. Spread across the servicers they would drift apart, and a caller
cannot tell "your bridge is offline" from "your light id is wrong" by reading
a message.

**A status is only half of it.** Hue reports application errors inside
exchanges it answered with HTTP 200, and those never become a status at all:
they stay in the typed response — `MutationResponse.errors` — because a
mutation that half succeeded has two halves to report and a status can only
carry one. What is here is the other case, where the Bridge could not be
reached, would not answer, or answered with a refusal. Even then the Bridge's
own words come along: a code says *which kind* of failure it was, and only
`invalid value, dimming.brightness, 101` says which field to fix.

Two rows of the error model have no source here yet, and are named so that
their absence is a decision rather than an oversight. `UNAUTHENTICATED` is
`hue_grpc.serving.interceptors`' to answer, because it is about the Gateway
Token and no Bridge failure can produce it. `PERMISSION_DENIED` has none at
all: this Gateway has one Gateway Token and no roles, so a caller either
authenticates or does not, and when authorization arrives it belongs beside
the token check rather than beside the Bridge.

This module lives above `hue_grpc.hue`, which talks to the Bridge and knows
nothing about gRPC.
"""

from __future__ import annotations

from dataclasses import dataclass

import grpc

from hue_grpc.codec import InvalidCommandError
from hue_grpc.hue import errors
from hue_grpc.hue.lights import InvalidLightIdError, LightNotFoundError
from hue_grpc.hue.pairing import LinkButtonNotPressedError
from hue_grpc.hue.transport import (
    BridgeResponseError,
    BridgeTimeoutError,
    BridgeUnreachableError,
)

__all__ = ["Status", "status_for"]


@dataclass(frozen=True)
class Status:
    """What one failure reaches a gRPC client as."""

    #: What a client branches on.
    code: grpc.StatusCode
    #: What a person reads. Never carries a secret: the only failures that
    #: reach here name resources, fields and HTTP statuses.
    message: str


def status_for(failure: Exception) -> Status:
    """The status and message a gRPC client should see for `failure`."""
    return Status(_code_for(failure), _message_for(failure))


def _code_for(failure: Exception) -> grpc.StatusCode:
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
    if status_code in (405, 501):
        # The path exists and this Bridge does not serve it that way: older
        # firmware, or a resource this model does not have. Retrying is
        # pointless and the request was not wrong, which is neither
        # UNAVAILABLE nor INVALID_ARGUMENT. Checked before the 5xx sweep
        # below, which would otherwise call 501 a transient fault.
        return grpc.StatusCode.UNIMPLEMENTED
    if status_code >= 500:
        # The Bridge is up enough to answer and not well enough to serve;
        # a client retrying with backoff is the right response.
        return grpc.StatusCode.UNAVAILABLE
    return grpc.StatusCode.INTERNAL


def _message_for(failure: Exception) -> str:
    """What to tell the client, in Hue's words where Hue supplied any.

    A failed CLIP exchange carries an error envelope, and it is the half of
    the answer a status code cannot hold: `bridge returned HTTP 400` does not
    tell anyone which field to fix.
    """
    if isinstance(failure, BridgeResponseError):
        said = errors.descriptions(failure.payload)
        if said:
            return f"{failure}: {'; '.join(said)}"
    return str(failure)
