"""The Registry: the Gateway's own record of the Bridge it is paired with.

One Registry Entry, in one JSON file, under systemd's `StateDirectory`. It
holds the Bridge's identity and address, what the Bridge said about itself,
when it was last reached, and the two secrets Pairing minted. It lives outside
the Nix store deliberately: a system rollback replaces the store and must not
be able to take the Application Key with it, because re-minting one costs a
walk to the Bridge and a button press.

**Not encrypted at rest.** With `DynamicUser=true` and `StateDirectory` at
0700, anything that can read this file is already root or the service itself,
so a decryption key sitting on the same disk would be theatre.

Writes are atomic: a temp file in the same directory, `fsync`, `os.replace`,
then `fsync` of the directory. The last step is the one that is easy to
forget and the reason the whole dance is here — without it the rename itself
can be lost on power loss, and this is state that has to outlive that.

Reads never guess. A file that is damaged, or written by a Gateway newer than
this one, is an error and not an empty Registry: reporting "nothing is
registered" for a file we simply could not read would send the Gateway off to
pair again and orphan a perfectly good Application Key on the Bridge.

This module sits above `hue_grpc.hue`, which talks to the Bridge: the Registry
is what the Gateway knows, not what the Bridge serves.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "REGISTRY_FILE_MODE",
    "REGISTRY_FILE_NAME",
    "SCHEMA_VERSION",
    "STATE_DIRECTORY_ENV",
    "STATE_DIRECTORY_MODE",
    "Registry",
    "RegistryEntry",
    "RegistryError",
    "UnreadableRegistryError",
    "UnsupportedRegistryVersionError",
    "default_registry_path",
    "state_directory",
]

#: The file format's version. Bump it only for a change this Gateway's reader
#: could not make sense of; a Gateway refuses any version but its own.
SCHEMA_VERSION = 1

#: The single file, inside the state directory.
REGISTRY_FILE_NAME = "registry.json"

#: Owner-only. The file holds the Application Key in the clear.
REGISTRY_FILE_MODE = 0o600

#: Owner-only, matching what systemd creates `StateDirectory` as.
STATE_DIRECTORY_MODE = 0o700

#: systemd exports this for each `StateDirectory=` entry.
STATE_DIRECTORY_ENV = "STATE_DIRECTORY"

_DIRECTORY_NAME = "hue-grpc"

_log = logging.getLogger(__name__)


class RegistryError(Exception):
    """The Registry could not be read."""


class UnreadableRegistryError(RegistryError):
    """A Registry file exists but is not a Registry file this Gateway can read.

    Damaged, truncated, or holding something other than an entry. Distinct
    from there being no file at all, which is how every Gateway starts.
    """


class UnsupportedRegistryVersionError(RegistryError):
    """The file was written by a Gateway newer than this one.

    The rollback case. Reading it as if it were the format we know would
    either drop fields or misread them, so it is refused by name instead.
    """

    def __init__(self, found: object) -> None:
        super().__init__(
            f"registry is version {found!r}, this gateway reads version "
            f"{SCHEMA_VERSION}; it was written by a newer gateway"
        )
        self.found = found


@dataclass(frozen=True, repr=False)
class RegistryEntry:
    """What the Gateway knows about one Bridge.

    Frozen, because an entry is a snapshot of what was last written down.
    Change one with `dataclasses.replace`: following a Bridge to a new address
    means a new entry carrying the same secrets, never a re-Pairing.
    """

    #: The Bridge's permanent identity, held uppercase: one canonical form, so
    #: the same Bridge cannot be written down two ways across saves. This is
    #: what `hue_grpc.hue.tls` compares a certificate's common name against,
    #: but that comparison casefolds both sides — the case here is for the
    #: Registry's own benefit, not for the TLS check.
    bridge_id: str
    #: Where the Bridge was last found. Expected to change; the Bridge ID is
    #: what makes following it there safe.
    address: str
    #: What the Bridge Resource called its model, e.g. `BSB002`. `None` until
    #: the Bridge has been asked.
    model: str | None
    #: The Bridge's firmware version, likewise.
    firmware: str | None
    #: When the Bridge was last reached successfully, in UTC. `None` before
    #: the first successful contact. Written at coarse moments — this is a
    #: whole-file rewrite with two fsyncs, not a per-request counter.
    last_contact: dt.datetime | None
    #: The Application Key, sent as `hue-application-key`. An entry always has
    #: one: Registration is what happens after Pairing succeeded.
    application_key: str
    #: The Client Key, for Entertainment streaming. `None` when the Bridge
    #: minted none, which costs another button press to put right.
    client_key: str | None

    def __post_init__(self) -> None:
        if not self.bridge_id.strip():
            raise ValueError("registry entry needs a bridge id")
        if not self.address.strip():
            raise ValueError("registry entry needs an address")
        if not self.application_key:
            raise ValueError("registry entry needs an application key")
        if self.last_contact is not None and self.last_contact.tzinfo is None:
            # A naive moment on disk would be read as whatever the host's
            # clock happens to mean the next time it is read.
            raise ValueError("last contact needs a timezone")
        object.__setattr__(self, "bridge_id", self.bridge_id.strip().upper())
        if self.last_contact is not None:
            object.__setattr__(
                self, "last_contact", self.last_contact.astimezone(dt.UTC)
            )

    def __repr__(self) -> str:
        """Everything but the secrets: an entry travels through tracebacks."""
        client_key = "<redacted>" if self.client_key is not None else None
        return (
            f"RegistryEntry(bridge_id={self.bridge_id!r}, "
            f"address={self.address!r}, model={self.model!r}, "
            f"firmware={self.firmware!r}, last_contact={self.last_contact!r}, "
            f"application_key=<redacted>, client_key={client_key})"
        )


def state_directory(environ: Mapping[str, str] | None = None) -> Path:
    """Where the Gateway keeps state that must outlive the Nix store.

    Under systemd that is `StateDirectory`, which arrives as a colon-separated
    list — one path per configured entry, ours first. Outside systemd, where
    `/var/lib` is not ours to write to, state goes under XDG's state home so
    that running the Gateway by hand needs no privileges.
    """
    environ = os.environ if environ is None else environ
    configured = environ.get(STATE_DIRECTORY_ENV)
    if configured:
        # A literal colon, not os.pathsep: this is systemd's format for
        # the variable, not the host's convention for search paths.
        return Path(configured.split(":")[0])
    home = environ.get("XDG_STATE_HOME")
    base = Path(home) if home else Path.home() / ".local" / "state"
    return base / _DIRECTORY_NAME


def default_registry_path(environ: Mapping[str, str] | None = None) -> Path:
    """The Registry file inside the state directory."""
    return state_directory(environ) / REGISTRY_FILE_NAME


class Registry:
    """One Registry Entry, persisted at `path`.

    One writer is assumed: the Gateway is a single service, and two of them
    sharing a state directory is out of scope along with multiple Bridges.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> RegistryEntry | None:
        """The stored entry, or `None` when nothing has been registered yet.

        `None` means exactly one thing: there is no file. Anything else that
        stops the entry being read is raised, because a Gateway that treats an
        unreadable Registry as an empty one pairs again and leaves the old
        Application Key stranded on the Bridge.
        """
        try:
            document = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as unreadable:
            raise UnreadableRegistryError(
                f"could not read registry at {self._path}: {unreadable}"
            ) from unreadable
        return _entry(_bridge(document))

    def save(self, entry: RegistryEntry) -> None:
        """Write `entry`, atomically, or leave what was there untouched.

        The temp file shares the destination's directory so the rename stays
        within one filesystem, and `mkstemp` creates it 0600 whatever the
        umask says. `os.replace` carries that mode across, so a file someone
        loosened by hand is tightened again by the next save.
        """
        directory = self._path.parent
        directory.mkdir(mode=STATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            dir=directory, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        temp_path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temp_file:
                temp_file.write(_document(entry))
                temp_file.flush()
                # The bytes reach the disk before any name points at them.
                os.fsync(temp_file.fileno())
            os.replace(temp_path, self._path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        # And the directory holding that name is flushed too, or the rename
        # itself is what a power loss takes.
        _fsync_directory(directory)
        _log.info(
            "wrote registry entry for bridge %s at %s", entry.bridge_id, entry.address
        )


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _document(entry: RegistryEntry) -> str:
    """The file's contents. Indented, because a human will read it in anger."""
    document = {
        "version": SCHEMA_VERSION,
        "bridge": {
            "id": entry.bridge_id,
            "address": entry.address,
            "model": entry.model,
            "firmware": entry.firmware,
            "last_contact": (
                None if entry.last_contact is None else entry.last_contact.isoformat()
            ),
            "application_key": entry.application_key,
            "client_key": entry.client_key,
        },
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def _bridge(document: str) -> Mapping[str, Any]:
    """The bridge object out of a Registry file, or why it could not be had."""
    try:
        payload = json.loads(document)
    except (json.JSONDecodeError, UnicodeDecodeError) as undecodable:
        raise UnreadableRegistryError(
            f"registry is not valid JSON: {undecodable}"
        ) from undecodable
    if not isinstance(payload, Mapping):
        raise UnreadableRegistryError(
            f"registry holds {type(payload).__name__}, expected an object"
        )
    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise UnreadableRegistryError("registry does not say which version it is")
    if version != SCHEMA_VERSION:
        raise UnsupportedRegistryVersionError(version)
    bridge = payload.get("bridge")
    if not isinstance(bridge, Mapping):
        raise UnreadableRegistryError("registry holds no bridge")
    return bridge


def _entry(bridge: Mapping[str, Any]) -> RegistryEntry:
    """A `RegistryEntry` from a file's bridge object, insisting on its shape."""
    try:
        return RegistryEntry(
            bridge_id=_text(bridge, "id"),
            address=_text(bridge, "address"),
            model=_optional_text(bridge, "model"),
            firmware=_optional_text(bridge, "firmware"),
            last_contact=_moment(bridge, "last_contact"),
            application_key=_text(bridge, "application_key"),
            client_key=_optional_text(bridge, "client_key"),
        )
    except ValueError as invalid:
        # Everything RegistryEntry refuses to be constructed with, reported as
        # a bad file rather than as a programming error.
        raise UnreadableRegistryError(f"registry entry is not usable: {invalid}") from (
            invalid
        )


def _text(bridge: Mapping[str, Any], field: str) -> str:
    value = bridge.get(field)
    if not isinstance(value, str):
        raise UnreadableRegistryError(
            f"registry entry has no {field}"
            if value is None
            else f"registry entry's {field} is {type(value).__name__}, expected text"
        )
    return value


def _optional_text(bridge: Mapping[str, Any], field: str) -> str | None:
    if bridge.get(field) is None:
        return None
    return _text(bridge, field)


def _moment(bridge: Mapping[str, Any], field: str) -> dt.datetime | None:
    value = _optional_text(bridge, field)
    if value is None:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError as unparseable:
        raise UnreadableRegistryError(
            f"registry entry's {field} is not a timestamp: {value!r}"
        ) from unparseable
