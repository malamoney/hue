"""The three Bridge calls the lighting RPCs are made of.

CLIP v2 answers every request with the same envelope — a `data` array and an
`errors` array — and a mutation can fill both at once, having done part of
what was asked and refused the rest. That is why `change` hands back both
rather than a value or an exception: the failure is part of the answer, not
instead of it.

Everything here speaks JSON. Turning a Resource into protobuf is
`hue_grpc.codec`'s job and happens a layer up, so this module stays what
`hue_grpc.hue` is for: talking to the Bridge.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from hue_grpc.hue import errors
from hue_grpc.hue.transport import (
    BridgeResponseError,
    HueTransport,
    MalformedResponseError,
)

__all__ = [
    "LIGHT_COLLECTION",
    "InvalidLightIdError",
    "LightNotFoundError",
    "Lights",
    "Mutation",
]

#: Where CLIP v2 keeps lights.
LIGHT_COLLECTION = "/clip/v2/resource/light"

#: What a Resource id is allowed to be. Hue issues UUIDs, and this is wider
#: than that on purpose — but not wide enough to hold a `/`, a `?`, a `.` or a
#: space, because the id is concatenated into a URL path and one that could
#: steer the request somewhere else is not an id.
_RESOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

_log = logging.getLogger(__name__)


class InvalidLightIdError(ValueError):
    """The id could not be part of a URL path, so no request was made."""


class LightNotFoundError(LookupError):
    """The Bridge has no light with that id."""


@dataclass(frozen=True)
class Mutation:
    """What a change did, and what it did not do.

    Both lists can be non-empty at once. A Bridge that turned one light on and
    could not reach another says so in a single successful exchange.
    """

    #: The Resources the Bridge reports having changed, as CLIP v2 resource
    #: identifiers — `{"rid": ..., "rtype": ...}`.
    updated: list[Mapping[str, Any]] = field(default_factory=list)
    #: Hue's own error objects, in Hue's words.
    errors: list[Mapping[str, Any]] = field(default_factory=list)


class Lights:
    """The light Resources on one Bridge."""

    def __init__(self, transport: HueTransport) -> None:
        self._transport = transport

    @property
    def transport(self) -> HueTransport:
        """The connection to the Bridge, for whoever is to close it."""
        return self._transport

    async def all(self) -> list[Mapping[str, Any]]:
        """Every light the Bridge knows about."""
        payload = await self._transport.request("GET", LIGHT_COLLECTION)
        _report(payload, LIGHT_COLLECTION)
        return _data(payload, LIGHT_COLLECTION)

    async def one(self, light_id: str) -> Mapping[str, Any]:
        """One light, or `LightNotFoundError` if the Bridge has no such id."""
        path = self._path(light_id)
        try:
            payload = await self._transport.request("GET", path)
        except BridgeResponseError as refused:
            if refused.status_code == 404:
                raise LightNotFoundError(f"the bridge has no light {light_id}") from (
                    refused
                )
            raise
        _report(payload, path)
        data = _data(payload, path)
        if not data:
            # A 200 with an empty collection. Hue answers 404 for an id it
            # does not have, so this is belt and braces — but reporting it as
            # a light that exists and has no properties would be worse.
            raise LightNotFoundError(f"the bridge has no light {light_id}")
        return data[0]

    async def change(self, light_id: str, command: Mapping[str, Any]) -> Mutation:
        """Send `command` to one light and report what the Bridge made of it.

        Never retried. A `PUT` that failed after the Bridge acted on it cannot
        be told apart from one that failed before, and repeating it would be
        the Gateway deciding to change the lights twice. `hue_grpc.hue.retry`
        is where that holds for every mutation rather than only this one.
        """
        path = self._path(light_id)
        payload = await self._transport.request("PUT", path, json=dict(command))
        return Mutation(updated=_data(payload, path), errors=_errors(payload, path))

    def _path(self, light_id: str) -> str:
        if not _RESOURCE_ID.fullmatch(light_id):
            raise InvalidLightIdError(
                f"{light_id!r} is not a resource id: they are up to 64 letters, "
                f"digits, underscores and dashes"
            )
        return f"{LIGHT_COLLECTION}/{light_id}"


def _report(payload: Any, path: str) -> None:
    """Say out loud what a read's envelope complained about.

    A read has no half-succeeded half to report: `LightGet` is a Resource, not
    an envelope, so an error the Bridge attached to a collection it answered
    has nowhere on the wire to go. Logging it is not as good as returning it
    and is much better than a Bridge complaining into a Gateway that never
    mentions it. A mutation, which does have somewhere to put them, returns
    them instead.
    """
    for description in errors.descriptions(payload):
        _log.warning("bridge reported an error reading %s: %s", path, description)


def _data(payload: Any, path: str) -> list[Mapping[str, Any]]:
    """The `data` array of a CLIP v2 envelope, insisting it is one."""
    return _array(payload, path, "data", required=True)


def _errors(payload: Any, path: str) -> list[Mapping[str, Any]]:
    """The `errors` array, which a Bridge with nothing to report may omit."""
    return _array(payload, path, "errors", required=False)


def _array(
    payload: Any, path: str, name: str, *, required: bool
) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise MalformedResponseError(
            f"{path} answered with {type(payload).__name__}, expected an object"
        )
    if name not in payload:
        if required:
            raise MalformedResponseError(f"{path} answered without a {name} array")
        return []
    entries = payload[name]
    if not isinstance(entries, list):
        raise MalformedResponseError(
            f"{path} answered with a {name} that is "
            f"{type(entries).__name__}, expected a list"
        )
    kept = [entry for entry in entries if isinstance(entry, Mapping)]
    if len(kept) != len(entries):
        # Dropping them silently would turn a Bridge that answered oddly into
        # a Gateway that answered wrongly.
        raise MalformedResponseError(
            f"{path} answered with a {name} holding something that is not an object"
        )
    return kept
