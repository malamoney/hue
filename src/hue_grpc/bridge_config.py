"""The bridge a declaratively-installed Gateway talks to.

A Gateway installed from the NixOS module has no Registry file and never runs
`pair`. It is told which Bridge to reach by configuration — the address and
the Bridge ID, neither of them secret — and handed the Application Key by a
systemd credential, a file that exists only while the unit runs and never
lands in the Nix store.

This module turns those two things into the same `RegistryEntry` the rest of
the Gateway already serves, without reading or writing `registry.json`. The
two are deliberately separate modes: `--bridge-address` selects this one, and
when it is set the Registry file is not consulted at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hue_grpc.registry import RegistryEntry

__all__ = [
    "APPLICATION_KEY",
    "CLIENT_KEY",
    "CredentialsFileError",
    "HueCredentials",
    "load_hue_credentials",
    "static_entry",
]

#: The one required key in the credentials file: the `hue-application-key`
#: header value, minted by an earlier Pairing done somewhere else.
APPLICATION_KEY = "application-key"

#: The optional second key, for Entertainment streaming. Absent whenever the
#: Pairing that minted the Application Key did not ask for one.
CLIENT_KEY = "client-key"

_KNOWN_KEYS = frozenset({APPLICATION_KEY, CLIENT_KEY})


class CredentialsFileError(Exception):
    """The Hue credentials file is missing, unreadable, or malformed.

    Raised on the way up, before anything binds, so a unit that can never
    reach its Bridge says why in the first line of its journal rather than
    starting and failing every call.
    """


@dataclass(frozen=True, repr=False)
class HueCredentials:
    """The secrets a static bridge needs, read from a runtime credential."""

    application_key: str
    client_key: str | None = None

    def __repr__(self) -> str:
        """Neither secret in the string: credentials travel through tracebacks."""
        client = "<redacted>" if self.client_key is not None else None
        return f"HueCredentials(application_key=<redacted>, client_key={client})"


def load_hue_credentials(path: Path) -> HueCredentials:
    """Parse `path`, a file of `key=value` lines, into `HueCredentials`.

    Blank lines and lines starting with `#` are ignored. Keys are matched
    case-insensitively with `_` and `-` treated alike, so both
    `application-key` and `application_key` work. An unknown key is an error
    rather than a warning: it is as likely to be a typo hiding a secret the
    Gateway needed as it is to be harmless.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as unreadable:
        raise CredentialsFileError(
            f"could not read hue credentials file {path}: {unreadable}"
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
        name = key.strip().lower().replace("_", "-")
        if name not in _KNOWN_KEYS:
            raise CredentialsFileError(
                f"{path}:{number}: unknown key {key.strip()!r}; "
                f"expected one of {', '.join(sorted(_KNOWN_KEYS))}"
            )
        if name in values:
            raise CredentialsFileError(f"{path}:{number}: {name} set twice")
        values[name] = value.strip()

    if not values.get(APPLICATION_KEY):
        raise CredentialsFileError(f"{path}: no {APPLICATION_KEY}")
    return HueCredentials(
        application_key=values[APPLICATION_KEY],
        client_key=values.get(CLIENT_KEY) or None,
    )


def static_entry(
    *, bridge_id: str, address: str, credentials: HueCredentials
) -> RegistryEntry:
    """A `RegistryEntry` for a bridge described by configuration.

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
