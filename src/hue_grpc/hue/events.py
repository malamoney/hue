"""The Bridge's event stream: server-sent events in, Resource changes out.

One connection, held open, silent for hours at a time and then carrying
several changes in a single frame. This module is the part of that which is
still about the Bridge: framing, the CLIP envelope inside each frame, and the
timestamp the Bridge wrote. Fanning the result out to gRPC subscribers,
reconnecting, and announcing a Gap are `hue_grpc.events`'s.

**A frame the Gateway cannot read ends the connection.** That is deliberate
and it is the opposite of what this codebase does when *reading* a Resource,
where a property from newer firmware is ignored and the light stays readable.
An unreadable Resource costs one property; an unreadable frame costs an
unknown number of events, silently, which is the one failure the whole event
design exists to avoid. Failing hands the problem to the reconnect, which
announces a Gap and resyncs — the events are still lost, but nobody is left
believing they saw everything.

**`Last-Event-ID` is not sent, and the `id:` line is parsed for nothing.**
The Bridge offers `If-None-Match` to resume from a timestamp, and does not
say when that timestamp has fallen out of its buffer — so a resumed stream
looks exactly like a complete one and cannot be told from it. Resync is the
guarantee instead: see `CONTEXT.md`.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from hue_grpc.hue.transport import (
    BridgeTimeoutError,
    BridgeUnreachableError,
    HueTransport,
    MalformedResponseError,
)

__all__ = ["EVENT_STREAM", "BridgeEvent", "BridgeEvents"]

#: Where CLIP v2 serves the event stream. Not under `/clip/v2/resource`.
EVENT_STREAM = "/eventstream/clip/v2"

_ACCEPT = {"Accept": "text/event-stream"}

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BridgeEvent:
    """One Resource, changed, as the Bridge described it.

    The Bridge's own unit is coarser: one event names several Resources, and
    one frame carries several events. This is the finest grain that is still
    a whole fact — a client filters on the Resource, not on the frame it
    happened to arrive in — so `id` is shared by the changes that arrived
    together rather than unique to each.
    """

    #: The Bridge's id for the event this arrived in. Empty for a change the
    #: Gateway synthesised, which the Bridge never sent and never named.
    id: str
    #: `update`, `add`, `delete` or `error`, in Hue's spelling.
    type: str
    #: The Bridge's `creationtime`, or `None` when it sent none this Gateway
    #: could read. Never the Gateway's own clock: that is added on delivery,
    #: where it is honest about being a receive time.
    created: dt.datetime | None
    #: What changed, as CLIP reports it: `id` and `type`, plus the properties
    #: that moved. Only those — an event is a change, not a Resource.
    resource: Mapping[str, Any]

    @property
    def resource_id(self) -> str:
        return _text(self.resource.get("id"))

    @property
    def resource_type(self) -> str:
        return _text(self.resource.get("type"))


class BridgeEvents:
    """The event stream of one Bridge."""

    def __init__(self, transport: HueTransport) -> None:
        self._transport = transport

    @property
    def transport(self) -> HueTransport:
        """The connection to the Bridge, for whoever is to close it."""
        return self._transport

    @asynccontextmanager
    async def connected(self) -> AsyncIterator[AsyncIterator[list[BridgeEvent]]]:
        """Hold the stream open, handing over each frame as a batch.

        Connecting is entering the block, not taking the first batch. The
        Bridge says nothing at all between changes, so a caller that has to
        act on the connection being up — announcing a Gap, resyncing — would
        otherwise be waiting on an event that may be hours away.
        """
        async with self._transport.stream(
            "GET", EVENT_STREAM, headers=_ACCEPT
        ) as response:
            yield self._batches(response)

    async def _batches(
        self, response: httpx.Response
    ) -> AsyncIterator[list[BridgeEvent]]:
        async for frame in _frames(_lines(response)):
            yield _events(frame)


async def _lines(response: httpx.Response) -> AsyncIterator[str]:
    """The response body line by line, in this layer's failure terms.

    The body is read long after the request was sent, so the translation
    `HueTransport._send` does on the way out has to be done again here: a
    Bridge that goes away mid-stream raises out of the iteration rather than
    out of the call that opened it.
    """
    try:
        async for line in response.aiter_lines():
            yield line
    except httpx.TimeoutException as timeout:
        raise BridgeTimeoutError(str(timeout) or repr(timeout)) from timeout
    except httpx.TransportError as lost:
        raise BridgeUnreachableError(str(lost) or repr(lost)) from lost


async def _frames(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """The `data` payload of each complete server-sent event.

    A frame is complete when a blank line ends it, so a connection that dies
    mid-frame delivers nothing: half a payload is not a smaller payload.
    Comments — a lone `:`, which is how the Bridge keeps a quiet connection
    alive — and every field but `data` are dropped; see the module docstring
    for why `id` is among them.
    """
    payload: list[str] = []
    async for line in lines:
        if not line:
            if payload:
                yield "\n".join(payload)
            payload = []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if name == "data":
            payload.append(value.removeprefix(" "))


def _events(frame: str) -> list[BridgeEvent]:
    """One frame's worth of changes, flattened out of the CLIP envelope."""
    try:
        payload = json.loads(frame)
    except json.JSONDecodeError as undecodable:
        raise MalformedResponseError(
            f"{EVENT_STREAM} sent a frame that is not JSON"
        ) from undecodable
    if not isinstance(payload, list):
        raise MalformedResponseError(
            f"{EVENT_STREAM} sent a frame holding {_kind(payload)}, "
            f"expected a list of events"
        )
    return [change for event in payload for change in _changes(event)]


def _changes(event: Any) -> list[BridgeEvent]:
    if not isinstance(event, Mapping):
        raise MalformedResponseError(
            f"{EVENT_STREAM} sent an event that is {_kind(event)}, expected an object"
        )
    resources = event.get("data")
    if not isinstance(resources, list):
        raise MalformedResponseError(
            f"{EVENT_STREAM} sent an event whose data is {_kind(resources)}, "
            f"expected a list"
        )
    for resource in resources:
        if not isinstance(resource, Mapping):
            raise MalformedResponseError(
                f"{EVENT_STREAM} sent a resource that is {_kind(resource)}, "
                f"expected an object"
            )
    identifier = _text(event.get("id"))
    kind = _text(event.get("type"))
    created = _timestamp(event.get("creationtime"), identifier)
    return [
        BridgeEvent(id=identifier, type=kind, created=created, resource=resource)
        for resource in resources
    ]


def _timestamp(value: Any, event_id: str) -> dt.datetime | None:
    """The Bridge's `creationtime`, or `None` when it is not readable.

    Unlike the shape checks above, an unreadable timestamp loses nothing: the
    change is still the change, and the Gateway's receive time still bounds
    when it happened.
    """
    if not isinstance(value, str):
        return None
    try:
        when = dt.datetime.fromisoformat(value)
    except ValueError:
        _log.debug("event %s has an unreadable creationtime %r", event_id, value)
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=dt.UTC)


def _text(value: Any) -> str:
    """A string field of the envelope, or `""` where the Bridge sent none."""
    return value if isinstance(value, str) else ""


def _kind(value: Any) -> str:
    """What arrived, in JSON's words rather than Python's.

    `hue_grpc.codec` has the same six lines for the same purpose, and they
    stay apart deliberately: sharing them would mean this module importing
    the one that speaks protobuf, and nothing under `hue_grpc.hue` knows that
    protobuf exists.
    """
    return {
        type(None): "nothing",
        bool: "a boolean",
        int: "a whole number",
        float: "a number",
        str: "text",
        list: "a list",
        dict: "an object",
    }.get(type(value), f"a {type(value).__name__}")
