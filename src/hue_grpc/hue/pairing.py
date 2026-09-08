"""Pairing: asking a Bridge to mint an Application Key for this Gateway.

Pairing is the one exchange that predates the secret it produces, and the one
that speaks the v1 API: `POST /api` rather than a `/clip/v2/` path. It is also
the only moment a brand-new secret crosses the wire, which is why it goes
through the same verified transport as everything else — an unverified
handshake here would hand the mint to whatever answered on that address.

The Bridge answers HTTP 200 whether or not the button was pressed, so the
outcome lives in the body. Error type 101 means nobody has walked over to the
Bridge yet: an expected step in the process, reported as its own failure so
that callers can wait and ask again rather than treat it as a fault. The gRPC
status each failure surfaces as lives in `hue_grpc.status`, which keeps this
layer talking only to the Bridge.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hue_grpc.hue.transport import HueTransport

__all__ = [
    "APPLICATION_NAME",
    "INSTANCE_NAME_LIMIT",
    "LINK_BUTTON_NOT_PRESSED",
    "PAIRING_PATH",
    "LinkButtonNotPressedError",
    "MalformedPairingResponseError",
    "PairedSecrets",
    "PairingError",
    "PairingRejectedError",
    "device_type",
    "pair",
]

#: The v1 endpoint. Pairing never moved to CLIP v2.
PAIRING_PATH = "/api"

#: Hue's error type for "the link button has not been pressed".
LINK_BUTTON_NOT_PRESSED = 101

#: The application half of `devicetype`, shown on the Bridge's app list. The
#: v1 API allows twenty characters for it, of which this spends eight.
APPLICATION_NAME = "hue-grpc"

#: The device half of `devicetype`. The v1 API bounds the whole field at forty
#: characters and the device name at nineteen, and rejects a longer one with
#: error type 7, which reads as nothing in particular. Refusing it here says
#: which name was too long instead.
INSTANCE_NAME_LIMIT = 19

_log = logging.getLogger(__name__)


class PairingError(Exception):
    """The Bridge did not mint an Application Key."""


class PairingRejectedError(PairingError):
    """The Bridge refused to mint a key and said why."""

    def __init__(self, hue_error_type: int, description: str) -> None:
        super().__init__(
            f"bridge refused pairing: {description} (type {hue_error_type})"
        )
        self.hue_error_type = hue_error_type
        self.description = description


class LinkButtonNotPressedError(PairingRejectedError):
    """Nobody has pressed the Bridge's link button yet.

    The expected first outcome, not a fault: Pairing is a race between this
    call and someone walking to the Bridge.
    """


class MalformedPairingResponseError(PairingError):
    """The Bridge answered `POST /api` with neither a success nor an error."""


@dataclass(frozen=True, repr=False)
class PairedSecrets:
    """What one Pairing minted.

    Both fields are secrets, so the repr shows neither: this value travels
    through tracebacks and debug logs on its way to the Registry.
    """

    #: The Application Key, sent as `hue-application-key` on CLIP v2 requests.
    #: The v1 API calls this field `username`; it is not a username.
    application_key: str
    #: The Client Key for Entertainment streaming. Nothing uses it yet, and
    #: `None` when the Bridge minted none — which costs another button press
    #: to put right, so callers are made to look at it before using it.
    client_key: str | None

    def __repr__(self) -> str:
        client_key = "<redacted>" if self.client_key is not None else None
        return f"PairedSecrets(application_key=<redacted>, client_key={client_key})"


def device_type(instance: str) -> str:
    """The `devicetype` identifying this Gateway to the Bridge.

    `instance` names which Gateway is asking, so that a Bridge shared by more
    than one of them lists them separately.
    """
    if not instance.strip():
        raise ValueError("instance name must not be empty")
    if "#" in instance:
        # The Bridge splits devicetype on the first '#'; a second one would
        # silently rename the instance rather than fail.
        raise ValueError(f"instance name must not contain '#': {instance!r}")
    if len(instance) > INSTANCE_NAME_LIMIT:
        raise ValueError(
            f"instance name is {len(instance) - INSTANCE_NAME_LIMIT} characters "
            f"longer than the bridge's {INSTANCE_NAME_LIMIT}-character limit: "
            f"{instance!r}"
        )
    return f"{APPLICATION_NAME}#{instance}"


async def pair(transport: HueTransport, *, instance: str) -> PairedSecrets:
    """Ask the Bridge behind `transport` to mint an Application Key.

    On success the transport is left holding the new Application Key — the
    attribute `HueTransport` documents as Pairing's to set — so the caller can
    go straight on to CLIP v2 calls. Persisting the secrets into a Registry
    Entry is Registration's job, and a separate one.
    """
    devicetype = device_type(instance)
    _log.info("pairing with bridge as %s", devicetype)
    payload = await transport.request(
        "POST",
        PAIRING_PATH,
        # Asked for even though nothing reads the Client Key yet: adding it
        # later would cost another trip to the Bridge to press the button.
        json={"devicetype": devicetype, "generateclientkey": True},
    )
    secrets = _read_pairing_response(payload)
    transport.application_key = secrets.application_key
    _log.info("bridge minted an application key for %s", devicetype)
    return secrets


def _read_pairing_response(payload: Any) -> PairedSecrets:
    """Read the secrets out of a `POST /api` body, or say what went wrong."""
    for entry in _entries(payload):
        if "error" in entry:
            raise _rejection(entry["error"])
        if "success" in entry:
            return _secrets(entry["success"])
    raise MalformedPairingResponseError(
        "bridge answered pairing with neither a success nor an error"
    )


def _entries(payload: Any) -> list[Mapping[str, Any]]:
    """The response's entries, whether it came wrapped in a list or not.

    A Bridge answers with a list of one, which is the shape to expect. The
    bare object is accepted too because that is how issue #7 writes the
    refusal down, and the two are the same answer.
    """
    entries = [payload] if isinstance(payload, Mapping) else payload
    if not isinstance(entries, list):
        raise MalformedPairingResponseError(
            f"bridge answered pairing with {type(payload).__name__}, "
            "expected an object or a list"
        )
    return [entry for entry in entries if isinstance(entry, Mapping)]


def _rejection(error: Any) -> PairingError:
    """The failure a Hue v1 error envelope describes."""
    if not isinstance(error, Mapping) or not isinstance(error.get("type"), int):
        return MalformedPairingResponseError(
            "bridge refused pairing without saying which error it was"
        )
    hue_error_type = error["type"]
    description = str(error.get("description", "no description"))
    if hue_error_type == LINK_BUTTON_NOT_PRESSED:
        return LinkButtonNotPressedError(hue_error_type, description)
    return PairingRejectedError(hue_error_type, description)


def _secrets(success: Any) -> PairedSecrets:
    """The minted secrets, insisting on the one that is not optional."""
    application_key = success.get("username") if isinstance(success, Mapping) else None
    if not isinstance(application_key, str) or not application_key:
        raise MalformedPairingResponseError(
            "bridge reported a successful pairing without an application key"
        )
    client_key = success.get("clientkey")
    if not isinstance(client_key, str) or not client_key:
        # Not fatal, and not worth failing over: CLIP v2 never uses the Client
        # Key, and refusing the whole Pairing would throw away an Application
        # Key that has already cost a button press. Said out loud instead,
        # because obtaining the Client Key later costs another one.
        _log.warning(
            "bridge minted no client key; entertainment streaming would need "
            "pairing again"
        )
        client_key = None
    return PairedSecrets(application_key=application_key, client_key=client_key)
