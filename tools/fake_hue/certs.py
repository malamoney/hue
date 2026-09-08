"""Bridge-shaped certificates for the fake Hue Bridge.

A real Bridge presents an ECDSA P-256 leaf whose only identity is
``CN=<bridge id>`` — no subjectAltName — issued by a self-signed ``CN=root-bridge``
CA. The Gateway cannot verify that with a stock TLS stack, so it turns hostname
checking off and asserts the common name by hand; see
``docs/adr/0002-bridge-tls-verification.md``.

The fake mints a certificate of exactly that shape under a CA of its own. The
Gateway is pointed at that CA with ``--bridge-ca-file``, so the whole
verification path — CA trust *and* the common-name assertion — runs for real
against the fake, rather than being switched off for the test.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

__all__ = ["CA_COMMON_NAME", "BridgeCerts", "mint_bridge_certs"]

#: The issuer common name a real Bridge leaf carries, reproduced so the fake's
#: chain looks like the real one to anything that inspects it.
CA_COMMON_NAME = "root-bridge"

#: The fake's certificates are minted fresh on every start and thrown away on
#: stop, so a lifetime measured in days is only about tolerating clock skew
#: between the nodes of a test.
_VALIDITY = dt.timedelta(days=2)


@dataclass(frozen=True)
class BridgeCerts:
    """A CA and one Bridge leaf issued by it, written to disk as PEM."""

    #: The leaf's common name: the Bridge ID, lowercased, as a real Bridge
    #: states it.
    common_name: str
    ca_file: Path
    cert_file: Path
    key_file: Path

    @property
    def ca_pem(self) -> str:
        return self.ca_file.read_text(encoding="ascii")


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _name(common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "NL"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Philips Hue"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def mint_bridge_certs(directory: Path, *, bridge_id: str) -> BridgeCerts:
    """Issue a self-signed CA and a Bridge leaf under it, into ``directory``.

    The leaf's common name is ``bridge_id`` lowercased and it carries no
    subjectAltName, both deliberately: that is the shape the Gateway's manual
    identity check exists to cope with, and a leaf with a SAN would let a
    stock verification succeed and leave that check untested.
    """
    directory.mkdir(parents=True, exist_ok=True)
    common_name = bridge_id.lower()
    now = dt.datetime.now(tz=dt.UTC)

    ca_key = _key()
    ca_subject = _name(CA_COMMON_NAME)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _VALIDITY)
        .not_valid_after(now + _VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = _key()
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(ca_subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _VALIDITY)
        .not_valid_after(now + _VALIDITY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    ca_file = directory / "ca.pem"
    cert_file = directory / "bridge.pem"
    key_file = directory / "bridge.key"
    ca_file.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_file.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return BridgeCerts(
        common_name=common_name,
        ca_file=ca_file,
        cert_file=cert_file,
        key_file=key_file,
    )
