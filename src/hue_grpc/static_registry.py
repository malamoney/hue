"""A Registry Entry built from configuration instead of from the file.

A Gateway installed from the NixOS module has no Registry file and never runs
`pair`. It is told which Bridge to reach by configuration — the address and
the Bridge ID, neither of them secret — and handed the Bridge's secrets by a
Credentials File, a systemd credential that exists only while the unit runs
and never lands in the Nix store.

This module turns those into the same `RegistryEntry` the rest of the Gateway
already serves, without reading or writing `registry.json`. The two are
separate modes: `--bridge-address` selects this one, and when it is set the
Registry file is not consulted at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hue_grpc.registry import RegistryEntry

__all__ = [
    "APPLICATION_KEY",
    "CLIENT_KEY",
    "BridgeCredentials",
    "CredentialsFileError",
    "load_bridge_credentials",
    "static_entry",
]

#: The one required key in the Credentials File: the `hue-application-key`
#: header value, minted by an earlier Pairing done somewhere else.
APPLICATION_KEY = "application-key"

#: The optional second key, for Entertainment streaming. Absent whenever the
#: Pairing that minted the Application Key did not ask for one.
CLIENT_KEY = "client-key"

_KNOWN_KEYS = (APPLICATION_KEY, CLIENT_KEY)


class CredentialsFileError(Exception):
    """The Credentials File is missing, unreadable, or malformed.

    Raised on the way up, before anything binds, so a unit that can never
    reach its Bridge says why in the first line of its journal rather than
    starting and failing every call.
    """


@dataclass(frozen=True, repr=False)
class BridgeCredentials:
    """The secrets a statically configured Bridge needs."""

    application_key: str
    client_key: str | None = None

    def __repr__(self) -> str:
        """Neither secret in the string: credentials travel through tracebacks."""
        client = "<redacted>" if self.client_key is not None else None
        return f"BridgeCredentials(application_key=<redacted>, client_key={client})"


def load_bridge_credentials(path: Path) -> BridgeCredentials:
    """Parse `path`, a Credentials File of `key=value` lines.

    Blank lines and lines starting with `#` are ignored and surrounding
    whitespace is stripped, because the file is written by hand or by a
    secrets tool. The keys are `application-key` and, optionally,
    `client-key`, spelled exactly; an unknown key is an error rather than a
    warning, since it is as likely to be a typo hiding a key the Gateway
    needed as it is to be harmless.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as unreadable:
        raise CredentialsFileError(
            f"could not read credentials file {path}: {unreadable}"
        ) from unreadable

    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise CredentialsFileError(
                f"{path}:{number}: expected key=value, got {raw!r}"
            )
        name = key.strip()
        if name not in _KNOWN_KEYS:
            raise CredentialsFileError(
                f"{path}:{number}: unknown key {name!r}; expected one of "
                f"{', '.join(_KNOWN_KEYS)}"
            )
        if name in values:
            raise CredentialsFileError(f"{path}:{number}: {name} set twice")
        values[name] = value.strip()

    if not values.get(APPLICATION_KEY):
        raise CredentialsFileError(f"{path}: no {APPLICATION_KEY}")
    return BridgeCredentials(
        application_key=values[APPLICATION_KEY],
        client_key=values.get(CLIENT_KEY) or None,
    )


def static_entry(
    *, bridge_id: str, address: str, credentials: BridgeCredentials
) -> RegistryEntry:
    """A `RegistryEntry` for a Bridge described by configuration.

    `model`, `firmware` and `last_contact` are unknown until the Bridge has
    been reached, exactly as they are for a freshly paired entry. A
    `ValueError` from here means the address or Bridge ID was empty.
    """
    return RegistryEntry(
        bridge_id=bridge_id,
        address=address,
        model=None,
        firmware=None,
        last_contact=None,
        application_key=credentials.application_key,
        client_key=credentials.client_key,
    )
