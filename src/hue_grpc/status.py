"""The gRPC status a Gateway failure surfaces to the client.

Issue #11 owns the full mapping, including the Hue error envelopes that ride
inside successful HTTP exchanges. What lives here is the part Pairing settles,
because a Bridge reporting "the button has not been pressed" does so in a
response it called successful, so no table keyed on HTTP status can reach it.

It lives above `hue_grpc.hue`, which talks to the Bridge and knows nothing
about gRPC.
"""

from __future__ import annotations

import grpc

from hue_grpc.hue.pairing import LinkButtonNotPressedError, PairingError

__all__ = ["grpc_status_for"]


def grpc_status_for(failure: PairingError) -> grpc.StatusCode:
    """The status a gRPC client should see for `failure`."""
    if isinstance(failure, LinkButtonNotPressedError):
        # FAILED_PRECONDITION, not UNAVAILABLE: the caller can recover by
        # walking to the Bridge and asking again, and retrying before they do
        # cannot succeed. It also stays legible next to issue #11's table,
        # where UNAVAILABLE already means the Bridge could not be reached —
        # a client must be able to tell "press the button" from "your bridge
        # is offline" without reading the message.
        return grpc.StatusCode.FAILED_PRECONDITION
    # Anything else is the Gateway failing to pair for a reason it did not
    # anticipate, which is issue #11's "unexpected failure" row.
    return grpc.StatusCode.INTERNAL
