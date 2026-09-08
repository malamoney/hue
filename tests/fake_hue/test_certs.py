"""The fake Bridge's certificate has the shape a real Bridge's does."""

from __future__ import annotations

import ssl
from pathlib import Path

from conftest import BRIDGE_ID
from cryptography import x509
from cryptography.x509.oid import ExtensionOID, NameOID
from fake_hue.certs import CA_COMMON_NAME, mint_bridge_certs


def _common_name(name: x509.Name) -> str:
    return name.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value  # type: ignore[return-value]


def test_writes_a_ca_a_leaf_and_a_key(tmp_path: Path) -> None:
    certs = mint_bridge_certs(tmp_path, bridge_id=BRIDGE_ID)

    assert certs.ca_file.is_file()
    assert certs.cert_file.is_file()
    assert certs.key_file.is_file()


def test_the_leaf_is_named_for_the_bridge_lowercased(tmp_path: Path) -> None:
    certs = mint_bridge_certs(tmp_path, bridge_id=BRIDGE_ID)
    leaf = x509.load_pem_x509_certificate(certs.cert_file.read_bytes())

    assert _common_name(leaf.subject) == BRIDGE_ID.lower()
    assert _common_name(leaf.issuer) == CA_COMMON_NAME
    assert certs.common_name == BRIDGE_ID.lower()


def test_the_leaf_has_no_subject_alternative_name(tmp_path: Path) -> None:
    """The absence of a SAN is the whole reason the Gateway checks by hand."""
    certs = mint_bridge_certs(tmp_path, bridge_id=BRIDGE_ID)
    leaf = x509.load_pem_x509_certificate(certs.cert_file.read_bytes())

    with_san = [
        extension
        for extension in leaf.extensions
        if extension.oid == ExtensionOID.SUBJECT_ALTERNATIVE_NAME
    ]
    assert with_san == []


def test_the_leaf_chains_to_the_ca(tmp_path: Path) -> None:
    certs = mint_bridge_certs(tmp_path, bridge_id=BRIDGE_ID)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(certs.ca_file))
    # Loads without raising: the leaf verifies against this CA and nothing else.
    context.load_cert_chain(certs.cert_file, certs.key_file)
