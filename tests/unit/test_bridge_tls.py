"""The TLS handshake against a Bridge.

See docs/adr/0002-bridge-tls-verification.md: hostname checking is off because
the Bridge certificate has no subjectAltName and is reached by IP, so the
identity check is made explicitly against the certificate's common name.
"""

from __future__ import annotations

import asyncio
import hashlib
import ssl

import pytest
from conftest import BRIDGE_ID, BridgeCerts, FakeBridge, run

from hue_grpc.hue.tls import (
    BridgeIdentityError,
    bridge_ssl_context,
    root_bridge_ca_pem,
)


async def _handshake(bridge: FakeBridge, context: ssl.SSLContext) -> None:
    host, port = bridge.address.rsplit(":", 1)
    _, writer = await asyncio.open_connection(
        host, int(port), ssl=context, server_hostname=host
    )
    writer.close()


def test_accepts_a_certificate_naming_the_expected_bridge(
    bridge_certs: BridgeCerts,
) -> None:
    async def scenario() -> None:
        async with FakeBridge(bridge_certs) as bridge:
            # The Registry holds the Bridge ID uppercase, the certificate
            # states it lowercase; both name the same Bridge.
            context = bridge_ssl_context(BRIDGE_ID, ca_pem=bridge_certs.ca_pem)
            await _handshake(bridge, context)

    run(scenario())


def test_rejects_a_certificate_naming_a_different_bridge(
    bridge_certs: BridgeCerts,
) -> None:
    """A neighbouring Bridge's certificate is Philips-signed and still wrong."""

    async def scenario() -> None:
        async with FakeBridge(bridge_certs) as bridge:
            context = bridge_ssl_context("ECB5FAFFFE000000", ca_pem=bridge_certs.ca_pem)

            with pytest.raises(BridgeIdentityError) as rejection:
                await _handshake(bridge, context)

            assert bridge_certs.common_name in str(rejection.value)
            # Rejected during the handshake: no request ever reached the peer.
            assert bridge.requests == []

    run(scenario())


def test_rejects_a_certificate_from_an_unknown_authority(
    bridge_certs: BridgeCerts, other_certs: BridgeCerts
) -> None:
    """Same common name, different issuer: verification must still fail."""

    async def scenario() -> None:
        async with FakeBridge(other_certs) as bridge:
            context = bridge_ssl_context(BRIDGE_ID, ca_pem=bridge_certs.ca_pem)

            with pytest.raises(ssl.SSLCertVerificationError):
                await _handshake(bridge, context)

            assert bridge.requests == []

    run(scenario())


def test_vendored_root_ca_is_the_philips_bridge_authority() -> None:
    """A swapped or truncated CA file would silently widen what we trust.

    The fingerprint is that of the CA that verifies the real Bridge's
    certificate chain, taken with `openssl verify` against the Bridge itself.
    """
    der = ssl.PEM_cert_to_DER_cert(root_bridge_ca_pem())

    assert hashlib.sha256(der).hexdigest() == (
        "f0bd8e6509e82f774d63bc009d5388c969fe3dcf7d6d541d6351b72b898d8acf"
    )


def test_context_trusts_only_the_vendored_root_ca_by_default() -> None:
    """No system trust store: one Philips CA is the whole set of anchors."""
    context = bridge_ssl_context(BRIDGE_ID)

    assert context.get_ca_certs(binary_form=True) == [
        ssl.PEM_cert_to_DER_cert(root_bridge_ca_pem())
    ]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is False
