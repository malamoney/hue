"""The TLS context used for every connection to a Bridge.

See docs/adr/0002-bridge-tls-verification.md. In short: the Bridge presents a
certificate whose only identity is ``CN=<bridge id>``, with no subjectAltName,
issued by Philips' ``root-bridge`` CA, and it is reached by IP address. Stock
verification therefore cannot succeed, so hostname checking is turned off and
the certificate's common name is asserted against the expected Bridge ID
instead. Neither half works without the other.

The assertion runs inside the handshake rather than after it, because the
first thing a paired Gateway sends is the Application Key.
"""

from __future__ import annotations

import ssl
from functools import cache
from importlib import resources
from typing import Any

__all__ = ["BridgeIdentityError", "bridge_ssl_context", "root_bridge_ca_pem"]

#: Philips' Bridge CA, vendored from the certificate published at
#: https://developers.meethue.com/develop/application-design-guidance/using-https/
#: and confirmed against a real Bridge with `openssl verify`. It is
#: self-signed, `CN=root-bridge`, and valid until 2038.
_ROOT_BRIDGE_CA_FILE = "root-bridge.pem"


@cache
def root_bridge_ca_pem() -> str:
    """The vendored Philips `root-bridge` CA, in PEM form."""
    return (
        resources.files("hue_grpc.hue")
        .joinpath(_ROOT_BRIDGE_CA_FILE)
        .read_text(encoding="ascii")
    )


class BridgeIdentityError(Exception):
    """The peer's certificate does not name the Bridge we expected to reach."""


def _common_names(peer_cert: dict[str, Any] | None) -> list[str]:
    if not peer_cert:
        return []
    return [
        value
        for relative_name in peer_cert.get("subject", ())
        for attribute, value in relative_name
        if attribute == "commonName"
    ]


def _assert_bridge_identity(
    peer_cert: dict[str, Any] | None, expected_bridge_id: str
) -> None:
    """Fail unless the certificate names exactly the Bridge we expected."""
    names = _common_names(peer_cert)
    # The Bridge states its ID in lowercase; the Registry holds it uppercase.
    if len(names) == 1 and names[0].casefold() == expected_bridge_id.casefold():
        return
    raise BridgeIdentityError(
        f"certificate names {names or ['no bridge']}, "
        f"expected bridge {expected_bridge_id}"
    )


class _BridgeSSLObject(ssl.SSLObject):
    """An `SSLObject` that refuses to finish a handshake with the wrong Bridge."""

    def do_handshake(self) -> None:
        super().do_handshake()
        context: _BridgeSSLContext = self.context  # type: ignore[assignment]
        _assert_bridge_identity(self.getpeercert(), context.expected_bridge_id)


class _BridgeSSLSocket(ssl.SSLSocket):
    """The blocking-socket counterpart of `_BridgeSSLObject`."""

    def do_handshake(self, block: bool = False) -> None:
        super().do_handshake(block)
        context: _BridgeSSLContext = self.context  # type: ignore[assignment]
        _assert_bridge_identity(self.getpeercert(), context.expected_bridge_id)


class _BridgeSSLContext(ssl.SSLContext):
    """Carries the expected Bridge ID down into the handshake."""

    sslobject_class = _BridgeSSLObject
    sslsocket_class = _BridgeSSLSocket

    expected_bridge_id: str


def bridge_ssl_context(bridge_id: str, *, ca_pem: str | None = None) -> ssl.SSLContext:
    """Build the client context for connections to the Bridge with `bridge_id`.

    `ca_pem` is the trust anchor, defaulting to the vendored Philips
    `root-bridge` CA. The Bridge serves only its leaf certificate, so the CA
    never arrives over the wire and has to be supplied here.
    """
    context = _BridgeSSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.expected_bridge_id = bridge_id
    context.verify_mode = ssl.CERT_REQUIRED
    # Load-bearing, and safe only because of the common-name assertion that
    # _BridgeSSLObject makes on every handshake. Do not remove one without the
    # other: alone, this would accept any Philips-signed Bridge certificate.
    context.check_hostname = False
    context.load_verify_locations(cadata=ca_pem or root_bridge_ca_pem())
    return context
