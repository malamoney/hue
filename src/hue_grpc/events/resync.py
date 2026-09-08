"""Turning a Gap into known state.

A reconnect leaves the Gateway unable to say whether it missed anything — the
Bridge purges its event buffer without saying so, and no amount of care with
`If-None-Match` changes that. What the Gateway *can* do is ask what is true
now: re-read every Resource it models and say, as ordinary change events, how
that differs from what it last believed. The Gap stays unprovable and stops
mattering, for lights.

**Only for lights**, and that is the whole reason this works. Re-reading one
small collection after a reconnect is cheap; re-reading everything a Bridge
knows about would be a thundering herd aimed at the device that just stopped
answering. Resources the Gateway does not model are passed on when the Bridge
mentions them and never claimed to be known.

The snapshot follows the live stream as well as the reads, so a change that
arrived as an event is not reported a second time by the Resync after it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from hue_grpc.hue.events import BridgeEvent

__all__ = ["LIGHT", "Snapshot", "differences", "remember", "snapshot"]

#: The Hue Resource type this Gateway models, and so the only one a Resync
#: can speak for.
LIGHT = "light"

#: What the Gateway last believed about every light, by Resource id.
Snapshot = dict[str, dict[str, Any]]

#: Identity rather than state: present in every event, and never a change.
_IDENTITY = ("id", "type")


def snapshot(lights: Iterable[Mapping[str, Any]]) -> Snapshot:
    """What a full read of the light collection says, ready to be compared."""
    return {
        resource["id"]: dict(resource)
        for resource in lights
        if isinstance(resource.get("id"), str)
    }


def remember(known: Snapshot, event: BridgeEvent) -> None:
    """Fold one event into what the Gateway believes, in place.

    An event carries only the properties that moved, so an update is merged
    rather than assigned: a brightness change says nothing about whether the
    light is on, and overwriting would turn the silence into an answer.
    """
    if event.resource_type != LIGHT or not event.resource_id:
        return
    if event.type == "delete":
        known.pop(event.resource_id, None)
        return
    if event.type not in ("add", "update"):
        # An `error` event describes the Bridge's trouble with a Resource, not
        # a new value for one.
        return
    known.setdefault(event.resource_id, {"id": event.resource_id, "type": LIGHT})
    known[event.resource_id].update(event.resource)


def differences(before: Snapshot, after: Snapshot) -> list[BridgeEvent]:
    """The events that would have taken `before` to `after`.

    Synthesised, so they carry no Bridge event id and no Bridge timestamp:
    the Gateway knows the state is new to it and not when it changed. An
    update carries only the properties that differ, which is the shape the
    Bridge sends and the shape a subscriber already knows how to read.
    """
    changes = [
        _event("add", resource)
        for identifier, resource in after.items()
        if identifier not in before
    ]
    changes += [
        _event("update", {"id": identifier, "type": LIGHT, **moved})
        for identifier, resource in after.items()
        if identifier in before and (moved := _moved(before[identifier], resource))
    ]
    changes += [
        _event("delete", {"id": identifier, "type": LIGHT})
        for identifier in before
        if identifier not in after
    ]
    return changes


def _moved(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """The properties of `after` that `before` does not already say."""
    return {
        name: value
        for name, value in after.items()
        if name not in _IDENTITY and before.get(name) != value
    }


def _event(kind: str, resource: Mapping[str, Any]) -> BridgeEvent:
    return BridgeEvent(id="", type=kind, created=None, resource=resource)
