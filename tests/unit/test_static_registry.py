"""A Registry Entry built from configuration, the way the NixOS module does it.

The module hands the Gateway an address and a Bridge ID as flags and the
Bridge's secrets as a Credentials File — a `key=value` file under
`$CREDENTIALS_DIRECTORY`. These tests fix the file format and the shape of
the entry it becomes, because the module renders that file and nothing else
checks it end to end until the VM test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import BRIDGE_ID

from hue_grpc.static_registry import (
    BridgeCredentials,
    CredentialsFileError,
    load_bridge_credentials,
    static_entry,
)


def write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_an_application_key_on_its_own_is_a_complete_file(tmp_path: Path) -> None:
    credentials = load_bridge_credentials(
        write(tmp_path / "creds", "application-key=abc123\n")
    )

    assert credentials == BridgeCredentials(application_key="abc123", client_key=None)


def test_the_client_key_is_read_when_present(tmp_path: Path) -> None:
    credentials = load_bridge_credentials(
        write(tmp_path / "creds", "application-key=abc123\nclient-key=DEADBEEF\n")
    )

    assert credentials == BridgeCredentials(
        application_key="abc123", client_key="DEADBEEF"
    )


def test_blank_lines_comments_and_surrounding_whitespace_are_tolerated(
    tmp_path: Path,
) -> None:
    credentials = load_bridge_credentials(
        write(
            tmp_path / "creds",
            "# minted 2026-09-01\n\napplication-key = abc123 \n\n# no client key\n",
        )
    )

    assert credentials.application_key == "abc123"


def test_an_empty_client_key_is_the_same_as_none(tmp_path: Path) -> None:
    credentials = load_bridge_credentials(
        write(tmp_path / "creds", "application-key=abc123\nclient-key=\n")
    )

    assert credentials.client_key is None


def test_a_file_with_no_application_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CredentialsFileError, match="no application-key"):
        load_bridge_credentials(write(tmp_path / "creds", "client-key=DEADBEEF\n"))


def test_a_line_that_is_not_key_equals_value_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CredentialsFileError, match="expected key=value"):
        load_bridge_credentials(
            write(tmp_path / "creds", "application-key=abc123\ngarbage\n")
        )


def test_an_unknown_key_is_refused_rather_than_ignored(tmp_path: Path) -> None:
    with pytest.raises(CredentialsFileError, match="unknown key 'application_key'"):
        load_bridge_credentials(write(tmp_path / "creds", "application_key=abc123\n"))


def test_the_same_key_twice_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CredentialsFileError, match="set twice"):
        load_bridge_credentials(
            write(
                tmp_path / "creds",
                "application-key=abc123\napplication-key=def456\n",
            )
        )


def test_a_missing_file_says_so_without_a_traceback(tmp_path: Path) -> None:
    with pytest.raises(CredentialsFileError, match="could not read"):
        load_bridge_credentials(tmp_path / "absent")


def test_neither_secret_appears_in_the_repr(tmp_path: Path) -> None:
    credentials = load_bridge_credentials(
        write(
            tmp_path / "creds",
            "application-key=super-secret\nclient-key=also-secret\n",
        )
    )

    shown = repr(credentials)
    assert "super-secret" not in shown
    assert "also-secret" not in shown
    assert "<redacted>" in shown


def test_the_entry_looks_exactly_like_a_freshly_paired_one() -> None:
    entry = static_entry(
        bridge_id=BRIDGE_ID,
        address="192.168.86.223",
        credentials=BridgeCredentials(application_key="abc123", client_key="DEADBEEF"),
    )

    assert entry.bridge_id == BRIDGE_ID
    assert entry.address == "192.168.86.223"
    assert entry.application_key == "abc123"
    assert entry.client_key == "DEADBEEF"
    assert entry.model is None
    assert entry.firmware is None
    assert entry.last_contact is None


def test_an_empty_address_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="address"):
        static_entry(
            bridge_id=BRIDGE_ID,
            address="",
            credentials=BridgeCredentials(application_key="abc123"),
        )
