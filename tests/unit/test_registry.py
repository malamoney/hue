"""The Registry: what the Gateway knows about its Bridge, on disk.

These tests care about two things the file format alone cannot state. One is
that a write is all-or-nothing — a crash between the temp file and the rename,
or between the rename and the directory being flushed, must leave the previous
Registry Entry intact rather than a truncated one. The other is that a damaged
file is never mistaken for an empty Registry: the difference between "nothing
is registered" and "the record is unreadable" is the difference between
pairing for the first time and orphaning an Application Key on the Bridge.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import BRIDGE_ID

from hue_grpc.registry import (
    REGISTRY_FILE_NAME,
    SCHEMA_VERSION,
    Registry,
    RegistryEntry,
    UnreadableRegistryError,
    UnsupportedRegistryVersionError,
    default_registry_path,
    state_directory,
)

CONTACT = dt.datetime(2026, 9, 7, 18, 30, tzinfo=dt.UTC)


def an_entry(**overrides: object) -> RegistryEntry:
    fields: dict[str, object] = {
        "bridge_id": BRIDGE_ID,
        "address": "192.168.86.223",
        "model": "BSB002",
        "firmware": "1.68.0",
        "last_contact": CONTACT,
        "application_key": "an-application-key",
        "client_key": "a-client-key",
    }
    fields.update(overrides)
    return RegistryEntry(**fields)  # type: ignore[arg-type]


def a_registry(tmp_path: Path) -> Registry:
    return Registry(tmp_path / "state" / REGISTRY_FILE_NAME)


def test_a_saved_entry_loads_back_unchanged(tmp_path: Path) -> None:
    registry = a_registry(tmp_path)
    entry = an_entry()

    registry.save(entry)

    assert registry.load() == entry


def test_nothing_registered_yet_is_not_an_error(tmp_path: Path) -> None:
    """The first run has no file, and that is the normal way to start."""
    assert a_registry(tmp_path).load() is None


def test_the_optional_halves_of_an_entry_survive_being_absent(
    tmp_path: Path,
) -> None:
    """A Bridge that minted no Client Key, before its first contact."""
    registry = a_registry(tmp_path)
    entry = an_entry(model=None, firmware=None, last_contact=None, client_key=None)

    registry.save(entry)

    assert registry.load() == entry


def test_saving_again_replaces_the_previous_entry(tmp_path: Path) -> None:
    registry = a_registry(tmp_path)
    registry.save(an_entry())

    registry.save(an_entry(address="192.168.86.9"))

    loaded = registry.load()
    assert loaded is not None
    assert loaded.address == "192.168.86.9"
    # The address moved; the secret that cost a button press did not.
    assert loaded.application_key == "an-application-key"


def test_an_entry_outlives_the_process_that_wrote_it(tmp_path: Path) -> None:
    """A restart is the case this whole module exists for.

    Written by a separate interpreter, so nothing about the round trip can be
    an artefact of the entry still being in memory.
    """
    path = tmp_path / "state" / REGISTRY_FILE_NAME
    writer = subprocess.run(
        [
            sys.executable,
            "-c",
            "import datetime as dt, pathlib, sys;"
            "from hue_grpc.registry import Registry, RegistryEntry;"
            "Registry(pathlib.Path(sys.argv[1])).save(RegistryEntry("
            "bridge_id=sys.argv[2], address='192.168.86.223', model='BSB002',"
            "firmware='1.68.0', last_contact=dt.datetime(2026, 9, 7, 18, 30,"
            "tzinfo=dt.UTC), application_key='an-application-key',"
            "client_key='a-client-key'))",
            str(path),
            BRIDGE_ID,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert writer.returncode == 0, writer.stderr
    assert Registry(path).load() == an_entry()


def test_the_file_is_readable_only_by_its_owner(tmp_path: Path) -> None:
    registry = a_registry(tmp_path)

    registry.save(an_entry())

    assert stat.S_IMODE(registry.path.stat().st_mode) == 0o600


def test_a_loosened_file_is_tightened_by_the_next_save(tmp_path: Path) -> None:
    """The rename carries the temp file's mode over whatever was there."""
    registry = a_registry(tmp_path)
    registry.save(an_entry())
    registry.path.chmod(0o644)

    registry.save(an_entry(address="192.168.86.9"))

    assert stat.S_IMODE(registry.path.stat().st_mode) == 0o600


def test_the_state_directory_is_created_private(tmp_path: Path) -> None:
    """systemd creates StateDirectory at 0700; a bare run must match it."""
    registry = a_registry(tmp_path)

    registry.save(an_entry())

    assert stat.S_IMODE(registry.path.parent.stat().st_mode) == 0o700


def test_a_write_that_fails_leaves_the_previous_entry_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The crash the whole temp-file dance exists to survive."""
    registry = a_registry(tmp_path)
    registry.save(an_entry())

    def crash(source: object, destination: object) -> None:
        raise OSError("power loss")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError, match="power loss"):
        registry.save(an_entry(address="192.168.86.9"))

    assert registry.load() == an_entry()
    # And nothing half-written was left behind to be found later.
    assert list(registry.path.parent.iterdir()) == [registry.path]


def test_the_entry_is_flushed_before_the_rename_and_the_directory_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the directory fsync the rename itself can be lost.

    That is the failure this whole issue is about: the Registry Entry has to
    survive a power loss, not merely a process exit.
    """
    registry = a_registry(tmp_path)
    directory = registry.path.parent
    directory.mkdir(mode=0o700, parents=True)
    events: list[object] = []
    real_fsync, real_replace = os.fsync, os.replace

    def record_fsync(descriptor: int) -> None:
        events.append(os.fstat(descriptor).st_ino)
        real_fsync(descriptor)

    def record_replace(source: str, destination: str) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "replace", record_replace)
    registry.save(an_entry())

    assert "replace" in events
    rename = events.index("replace")
    # The entry's own bytes are on disk before the name points at them...
    assert events[:rename] == [registry.path.stat().st_ino]
    # ...and the directory holding that name is flushed afterwards.
    assert directory.stat().st_ino in events[rename + 1 :]


def test_a_truncated_file_is_reported_not_read_as_an_empty_registry(
    tmp_path: Path,
) -> None:
    """Returning None here would re-pair and orphan the Application Key."""
    registry = a_registry(tmp_path)
    registry.save(an_entry())
    registry.path.write_text('{"version": 1, "brid', encoding="utf-8")

    with pytest.raises(UnreadableRegistryError):
        registry.load()


def test_an_empty_file_is_reported_rather_than_read_as_no_entry(
    tmp_path: Path,
) -> None:
    registry = a_registry(tmp_path)
    registry.save(an_entry())
    registry.path.write_text("", encoding="utf-8")

    with pytest.raises(UnreadableRegistryError):
        registry.load()


def test_an_entry_missing_its_application_key_is_refused(tmp_path: Path) -> None:
    registry = a_registry(tmp_path)
    registry.save(an_entry())
    document = json.loads(registry.path.read_text(encoding="utf-8"))
    del document["bridge"]["application_key"]
    registry.path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(UnreadableRegistryError):
        registry.load()


def test_a_mistyped_field_is_refused(tmp_path: Path) -> None:
    registry = a_registry(tmp_path)
    registry.save(an_entry())
    document = json.loads(registry.path.read_text(encoding="utf-8"))
    document["bridge"]["address"] = 3232235743
    registry.path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(UnreadableRegistryError):
        registry.load()


def test_a_file_written_by_a_newer_gateway_is_refused_by_name(
    tmp_path: Path,
) -> None:
    """The rollback case: an older Gateway meeting a newer file.

    Guessing at a format it does not know would either lose fields or
    misread them, and the Registry is the one thing a rollback must not damage.
    """
    registry = a_registry(tmp_path)
    registry.save(an_entry())
    document = json.loads(registry.path.read_text(encoding="utf-8"))
    document["version"] = SCHEMA_VERSION + 1
    registry.path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(UnsupportedRegistryVersionError):
        registry.load()


def test_a_file_that_does_not_say_which_version_it_is_is_refused(
    tmp_path: Path,
) -> None:
    registry = a_registry(tmp_path)
    registry.save(an_entry())
    document = json.loads(registry.path.read_text(encoding="utf-8"))
    del document["version"]
    registry.path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(UnreadableRegistryError):
        registry.load()


def test_the_file_on_disk_has_the_shape_we_promised(tmp_path: Path) -> None:
    """The format is a compatibility surface; changing it needs a new version."""
    registry = a_registry(tmp_path)

    registry.save(an_entry())

    assert json.loads(registry.path.read_text(encoding="utf-8")) == {
        "version": SCHEMA_VERSION,
        "bridge": {
            "id": BRIDGE_ID,
            "address": "192.168.86.223",
            "model": "BSB002",
            "firmware": "1.68.0",
            "last_contact": "2026-09-07T18:30:00+00:00",
            "application_key": "an-application-key",
            "client_key": "a-client-key",
        },
    }


def test_the_bridge_id_is_stored_in_one_canonical_form(tmp_path: Path) -> None:
    """However it arrives, one Bridge is written down exactly one way.

    The TLS common-name check in hue_grpc.hue.tls casefolds both sides and so
    does not depend on this; entries comparing equal across saves does.
    """
    registry = a_registry(tmp_path)

    registry.save(an_entry(bridge_id=BRIDGE_ID.lower()))

    loaded = registry.load()
    assert loaded is not None
    assert loaded.bridge_id == BRIDGE_ID


def test_an_entry_without_a_bridge_id_is_refused() -> None:
    with pytest.raises(ValueError, match="bridge id"):
        an_entry(bridge_id="  ")


def test_an_entry_without_an_address_is_refused() -> None:
    with pytest.raises(ValueError, match="address"):
        an_entry(address="")


def test_an_entry_without_an_application_key_is_refused() -> None:
    with pytest.raises(ValueError, match="application key"):
        an_entry(application_key="")


def test_last_contact_is_kept_in_utc(tmp_path: Path) -> None:
    """A moment written in one timezone must read the same in another."""
    registry = a_registry(tmp_path)
    elsewhere = dt.timezone(dt.timedelta(hours=-7))

    registry.save(an_entry(last_contact=CONTACT.astimezone(elsewhere)))

    loaded = registry.load()
    assert loaded is not None
    assert loaded.last_contact == CONTACT
    assert loaded.last_contact is not None
    assert loaded.last_contact.tzinfo == dt.UTC


def test_a_last_contact_without_a_timezone_is_refused() -> None:
    """Naive on disk would be read as whatever the host's clock means today."""
    with pytest.raises(ValueError, match="timezone"):
        an_entry(last_contact=dt.datetime(2026, 9, 7, 18, 30))


def test_the_secrets_stay_out_of_the_repr() -> None:
    """The entry travels through tracebacks on its way to and from disk."""
    shown = repr(an_entry())

    assert "an-application-key" not in shown
    assert "a-client-key" not in shown
    # What is safe to see is still there, or the redaction is a debugging tax.
    assert BRIDGE_ID in shown
    assert "192.168.86.223" in shown


def test_the_secrets_stay_out_of_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registry = a_registry(tmp_path)

    with caplog.at_level(logging.DEBUG, logger="hue_grpc.registry"):
        registry.save(an_entry())
        registry.load()

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "an-application-key" not in logged
    assert "a-client-key" not in logged


def test_the_state_directory_is_the_one_systemd_gave_us() -> None:
    """StateDirectory= arrives as a colon-separated list, one per entry."""
    directory = state_directory({"STATE_DIRECTORY": "/var/lib/hue-grpc:/var/lib/x"})

    assert directory == Path("/var/lib/hue-grpc")


def test_a_gateway_outside_systemd_keeps_its_state_under_xdg() -> None:
    directory = state_directory({"XDG_STATE_HOME": "/home/someone/.local/state"})

    assert directory == Path("/home/someone/.local/state/hue-grpc")


def test_the_registry_file_sits_in_the_state_directory() -> None:
    path = default_registry_path({"STATE_DIRECTORY": "/var/lib/hue-grpc"})

    assert path == Path("/var/lib/hue-grpc") / REGISTRY_FILE_NAME
