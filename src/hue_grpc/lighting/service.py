"""`hue.v1.LightingService`: the three RPCs, and what they answer with.

The servicer itself is thin on purpose. Reading a Resource is
`hue_grpc.codec`, reaching the Bridge is `hue_grpc.hue.lights`, and which
status a failure becomes is `hue_grpc.status`; what is left here is the shape
of each answer and the one decision none of those can make — that a Bridge
error inside a successful exchange belongs in the response rather than in the
status.

A Gateway with no Registry Entry still hosts this service. It answers
`FAILED_PRECONDITION`, which is a client being told what to do about it,
where an unregistered service would be `UNIMPLEMENTED` — a Gateway that does
not do lighting at all.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import grpc

from hue.v1 import lighting_pb2, lighting_service_pb2
from hue.v1 import lighting_service_pb2_grpc as lighting_grpc
from hue_grpc.codec import CodecError, decode, encode
from hue_grpc.hue.lights import InvalidLightIdError, LightNotFoundError, Lights
from hue_grpc.hue.transport import HueTransportError
from hue_grpc.lighting.limits import COMMAND_RANGES
from hue_grpc.logs import fields
from hue_grpc.serving.serve import HostedService
from hue_grpc.status import status_for

__all__ = ["SERVICE_NAME", "LightingServicer", "hosted_lighting_service"]

#: `hue.v1.LightingService`, taken from the descriptor rather than written
#: out, so health, reflection and the wire can never disagree about it.
SERVICE_NAME: str = lighting_service_pb2.DESCRIPTOR.services_by_name[
    "LightingService"
].full_name

_UNPAIRED = (
    "no bridge is registered; run `hue-grpc-server pair` with the bridge's "
    "address and press its link button"
)

_NOTHING_TO_CHANGE = (
    "the command asks for no change; set the fields to change, and leave the "
    "rest unset to leave them alone"
)

#: Everything a servicer turns into a status, named rather than caught by
#: their base classes: `LookupError` would also catch a `KeyError` from a bug
#: in this Gateway, and answering that with a status is how a bug becomes a
#: quiet INTERNAL instead of the traceback the observability interceptor logs
#: for anything it does not recognise.
_ANSWERABLE = (
    HueTransportError,
    CodecError,
    InvalidLightIdError,
    LightNotFoundError,
)

_log = logging.getLogger(__name__)


class LightingServicer(lighting_grpc.LightingServiceServicer):  # type: ignore[misc]
    """Lights on the one Bridge this Gateway is paired with."""

    def __init__(self, lights: Lights | None) -> None:
        #: `None` until something has paired. The service is hosted either
        #: way; see the module docstring.
        self._lights = lights

    async def ListLights(
        self, request: lighting_service_pb2.ListLightsRequest, context: Any
    ) -> lighting_service_pb2.ListLightsResponse:
        async with self._bridge(context) as lights:
            response = lighting_service_pb2.ListLightsResponse()
            for light in await lights.all():
                decode(light, response.lights.add())
            return response

    async def GetLight(
        self, request: lighting_service_pb2.GetLightRequest, context: Any
    ) -> lighting_pb2.LightGet:
        async with self._bridge(context) as lights:
            return decode(await lights.one(request.light_id), lighting_pb2.LightGet())

    async def UpdateLight(
        self, request: lighting_service_pb2.UpdateLightRequest, context: Any
    ) -> lighting_service_pb2.MutationResponse:
        async with self._bridge(context) as lights:
            command = encode(request.command, ranges=COMMAND_RANGES)
            if not command:
                # A PUT of `{}` would be answered cheerfully by the Bridge
                # and change nothing, which is a client bug reported as a
                # success. It is not the same as a field left unset in a
                # command that changes something else.
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT, _NOTHING_TO_CHANGE
                )
            changed = await lights.change(request.light_id, command)
            if changed.errors:
                # Not a failed RPC. The Bridge may have done part of what was
                # asked, and the answer carries both halves.
                _log.warning(
                    "bridge refused part of a change",
                    **fields(
                        rpc="UpdateLight",
                        light_id=request.light_id,
                        updated=len(changed.updated),
                        errors=len(changed.errors),
                    ),
                )
            return _mutation(changed.updated, changed.errors)

    @asynccontextmanager
    async def _bridge(self, context: Any) -> AsyncIterator[Lights]:
        """The Bridge to serve this call from, or the status to answer with."""
        if self._lights is None:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, _UNPAIRED)
            raise AssertionError("abort does not return")
        try:
            yield self._lights
        except _ANSWERABLE as failure:
            answer = status_for(failure)
            _log.warning(
                "rpc could not be answered",
                **fields(status=answer.code.name, failure=type(failure).__name__),
            )
            await context.abort(answer.code, answer.message)


def _mutation(
    updated: list[Mapping[str, Any]], errors: list[Mapping[str, Any]]
) -> lighting_service_pb2.MutationResponse:
    response = lighting_service_pb2.MutationResponse()
    for resource in updated:
        decode(resource, response.updated.add())
    for error in errors:
        decode(error, response.errors.add())
    return response


def hosted_lighting_service(lights: Lights | None) -> HostedService:
    """The service, ready for `running_gateway` to register and announce."""
    servicer = LightingServicer(lights)

    def register(server: grpc.aio.Server) -> None:
        lighting_grpc.add_LightingServiceServicer_to_server(servicer, server)

    return HostedService(name=SERVICE_NAME, register=register)
