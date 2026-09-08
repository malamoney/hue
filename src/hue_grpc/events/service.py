"""`hue.v1.EventService`: one subscriber's view of the Bridge's stream.

Thin, like `hue_grpc.lighting.service`. The fan-out, the reconnecting and the
Resync are `hue_grpc.events.fanout`'s; reading a Resource into protobuf is
`hue_grpc.codec`'s. What is here is the shape of a `HueEvent` and the two
decisions that are this layer's alone: which Resource types a subscriber may
filter on, and that a Gap is a message on the stream rather than the end of
one.

A gRPC stream ends when the client hangs up or the Gateway stops. It does not
end because the Bridge went away — that is what `Gap` is for, and a client
that has to redial to find out it missed something has learned it too late.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, cast

import grpc

from hue.v1 import common_pb2, event_service_pb2, events_pb2
from hue.v1 import event_service_pb2_grpc as event_grpc
from hue_grpc.codec import decode, hue_names, hue_numbers
from hue_grpc.events.fanout import Cause, Change, EventFanout, Filter, Gap, Notice
from hue_grpc.events.resync import LIGHT
from hue_grpc.logs import fields
from hue_grpc.serving.serve import HostedService
from hue_grpc.status import UNPAIRED

__all__ = ["SERVICE_NAME", "EventServicer", "hosted_event_service"]

#: `hue.v1.EventService`, taken from the descriptor rather than written out,
#: so health, reflection and the wire can never disagree about it.
SERVICE_NAME: str = event_service_pb2.DESCRIPTOR.services_by_name[
    "EventService"
].full_name

_RTYPE = common_pb2.ResourceIdentifier.Rtype.DESCRIPTOR

_EVENT_TYPE = events_pb2.Event.Type.DESCRIPTOR

_GAP_CAUSES = {
    Cause.RECONNECTED: event_service_pb2.Gap.CAUSE_RECONNECTED,
    Cause.SUBSCRIBER_BEHIND: event_service_pb2.Gap.CAUSE_SUBSCRIBER_BEHIND,
}

_log = logging.getLogger(__name__)


class EventServicer(event_grpc.EventServiceServicer):  # type: ignore[misc]
    """What is happening on the one Bridge this Gateway is paired with."""

    def __init__(self, fanout: EventFanout | None, *, bridge_id: str = "") -> None:
        #: `None` until something has paired. The service is hosted either
        #: way, for the reason `hue_grpc.lighting.service` gives.
        self._fanout = fanout
        self._bridge_id = bridge_id

    async def Subscribe(
        self, request: event_service_pb2.SubscribeRequest, context: Any
    ) -> AsyncIterator[event_service_pb2.HueEvent]:
        if self._fanout is None:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, UNPAIRED)
            raise AssertionError("abort does not return")
        wanted = await _wanted(request, context)
        async with self._fanout.subscribe(wanted) as subscription:
            _log.info(
                "subscriber joined the event stream",
                **fields(
                    resource_ids=len(wanted.resource_ids),
                    resource_types=sorted(wanted.resource_types),
                    subscribers=self._fanout.subscribers,
                ),
            )
            async for notice in subscription:
                yield _hue_event(notice, self._bridge_id)


async def _wanted(request: event_service_pb2.SubscribeRequest, context: Any) -> Filter:
    """The filter `request` asks for, or the status that says why it cannot.

    A filter nobody can match is refused rather than served: a subscriber
    handed a silent stream has no way to tell "nothing has happened" from
    "you asked for a type that does not exist".
    """
    for resource_id in request.resource_ids:
        if not resource_id:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "resource_ids holds an empty id, which no resource can be; "
                "leave the field empty to see every resource",
            )
    types = set()
    for rtype in request.resource_types:
        name = hue_names(_RTYPE).get(rtype)
        if name is None or rtype == 0:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, _unusable(rtype, name)
            )
            raise AssertionError("abort does not return")
        types.add(name)
    return Filter(
        resource_ids=frozenset(request.resource_ids),
        resource_types=frozenset(types),
    )


def _unusable(rtype: int, name: str | None) -> str:
    """Why a requested Resource type is not one this Gateway can filter on."""
    if name is None:
        return (
            f"resource_types holds {rtype}, which is not a resource type this "
            f"gateway knows"
        )
    return (
        "resource_types holds RTYPE_UNSPECIFIED, which asks for no type in "
        "particular; leave the field empty to see every type"
    )


def _event_type(spelling: str) -> events_pb2.Event.Type:
    """Hue's spelling of an event type, as the generated enum holds it.

    A `cast` rather than a call: the generated enum types as a subclass of
    `int` and is a wrapper object at runtime that cannot be constructed, and
    the number is what the field holds either way. A spelling this Gateway has
    never seen becomes `TYPE_UNSPECIFIED` rather than costing the client the
    event, which still names the Resource that moved.
    """
    return cast("events_pb2.Event.Type", hue_numbers(_EVENT_TYPE).get(spelling, 0))


def _hue_event(notice: Notice, bridge_id: str) -> event_service_pb2.HueEvent:
    event = event_service_pb2.HueEvent(bridge_id=bridge_id)
    event.gateway_time.FromDatetime(notice.received)
    if isinstance(notice, Gap):
        event.gap.CopyFrom(
            event_service_pb2.Gap(cause=_GAP_CAUSES[notice.cause], missed=notice.missed)
        )
    else:
        _fill_change(event.change, notice)
    return event


def _fill_change(change: event_service_pb2.ResourceChange, notice: Change) -> None:
    """One Resource change, with the Bridge's own words kept where they are."""
    change.event_id = notice.event.id
    change.type = _event_type(notice.event.type)
    decode(
        {"rid": notice.event.resource_id, "rtype": notice.event.resource_type},
        change.resource,
    )
    if notice.event.created is not None:
        change.bridge_time.FromDatetime(notice.event.created)
    if notice.event.resource_type == LIGHT:
        # Set even when nothing inside it is, so that "this is a light" is
        # readable from the oneof rather than from the string in `resource`.
        change.light.SetInParent()
        decode(notice.event.resource, change.light)


def hosted_event_service(
    fanout: EventFanout | None, *, bridge_id: str = ""
) -> HostedService:
    """The service and its upstream reader, for `running_gateway` to host."""
    servicer = EventServicer(fanout, bridge_id=bridge_id)

    def register(server: grpc.aio.Server) -> None:
        event_grpc.add_EventServiceServicer_to_server(servicer, server)

    return HostedService(
        name=SERVICE_NAME,
        register=register,
        # An unpaired Gateway has nothing to read from and no task to run.
        run=None if fanout is None else fanout.run,
    )
