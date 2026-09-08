"""Bridge-shaped certificates, a TLS listener, and a stand-in gRPC service.

The real Bridge presents an ECDSA P-256 leaf whose only identity is
``CN=<bridge id>`` — no subjectAltName — issued by ``CN=root-bridge``. Tests
mint certificates with exactly that shape rather than reusing the vendored
Philips CA, whose private key nobody outside Philips has.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import ssl
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Iterator,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from hue_grpc.logs import record_upstream_status

# The Bridge ID as the Registry records it: uppercase. The Bridge states it in
# lowercase in its certificate, which is why the check is case-insensitive.
BRIDGE_ID = "ECB5FAFFFE334703"


def run[T](main: Coroutine[Any, Any, T]) -> T:
    """Run one async scenario. Async tests, without an async test plugin."""
    return asyncio.run(main)


@dataclass(frozen=True)
class BridgeCerts:
    """A CA and one leaf certificate issued by it, on disk as PEM."""

    common_name: str
    ca_file: Path
    cert_file: Path
    key_file: Path

    @property
    def ca_pem(self) -> str:
        return self.ca_file.read_text()


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


def mint_bridge_certs(
    directory: Path,
    *,
    common_name: str = BRIDGE_ID.lower(),
    ca_common_name: str = "root-bridge",
) -> BridgeCerts:
    """Issue a self-signed CA and a Bridge leaf certificate under it."""
    directory.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(tz=dt.UTC)

    ca_key = _key()
    ca_subject = _name(ca_common_name)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
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
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        # No SubjectAlternativeName, deliberately: the Bridge has none either,
        # and its absence is the whole reason for the manual identity check.
        .sign(ca_key, hashes.SHA256())
    )

    ca_file = directory / "ca.pem"
    cert_file = directory / "leaf.pem"
    key_file = directory / "leaf.key"
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


@pytest.fixture
def bridge_certs(tmp_path: Path) -> BridgeCerts:
    return mint_bridge_certs(tmp_path / "bridge")


def _content_length(head: bytes) -> int:
    for line in head.split(b"\r\n"):
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            return int(value)
    return 0


class FakeBridge:
    """A TLS listener answering every request with one canned HTTP response.

    It records the bytes it receives, so a test can tell the difference between
    a request that was rejected during the handshake and one that reached the
    Bridge before anything noticed.
    """

    def __init__(
        self,
        certs: BridgeCerts,
        *,
        body: str = "{}",
        status: str = "200 OK",
        content_type: str = "application/json",
        respond: Callable[[asyncio.StreamWriter], Awaitable[None]] | None = None,
    ) -> None:
        self._certs = certs
        self._body = body
        self._status = status
        self._content_type = content_type
        self._respond = respond
        self._server: asyncio.Server | None = None
        self.requests: list[bytes] = []

    async def __aenter__(self) -> FakeBridge:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self._certs.cert_file, self._certs.key_file)
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, ssl=context
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def address(self) -> str:
        assert self._server is not None
        host, port = self._server.sockets[0].getsockname()[:2]
        return f"{host}:{port}"

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            body = await reader.readexactly(_content_length(head))
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            ssl.SSLError,
            OSError,
        ):
            # A client that rejects the certificate drops the connection
            # mid-handshake; that is a passing test, not a server error.
            return
        self.requests.append(head + body)

        if self._respond is not None:
            await self._respond(writer)
            return

        body = self._body.encode()
        writer.write(
            f"HTTP/1.1 {self._status}\r\n"
            f"Content-Type: {self._content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()


@pytest.fixture
def other_certs(tmp_path: Path) -> Iterator[BridgeCerts]:
    """A second, unrelated CA — for proving an unknown issuer is rejected."""
    yield mint_bridge_certs(tmp_path / "other")


# A stand-in service for the interceptor tests. No generated stubs exist yet —
# the first real service lands in issue #10 — so this registers raw byte
# handlers, which is all an interceptor ever sees anyway.

ECHO_SERVICE = "hue.v1.Echo"
ECHO = f"/{ECHO_SERVICE}/Echo"
ECHO_STREAM = f"/{ECHO_SERVICE}/EchoStream"
SLEEP = f"/{ECHO_SERVICE}/Sleep"
SLEEP_STREAM = f"/{ECHO_SERVICE}/SleepStream"
BOOM = f"/{ECHO_SERVICE}/Boom"
DENIED = f"/{ECHO_SERVICE}/Denied"
UPSTREAM = f"/{ECHO_SERVICE}/Upstream"
UNKNOWN = f"/{ECHO_SERVICE}/NoSuchMethod"


#: Methods that were cancelled rather than allowed to finish. A shutdown that
#: only closed the door would leave this empty.
cancellations: list[str] = []


def echo_handlers() -> dict[str, Any]:
    async def echo(request: bytes, context: object) -> bytes:
        return request

    async def echo_stream(request: bytes, context: object) -> Any:
        for _ in range(3):
            yield request

    async def sleep(request: bytes, context: object) -> bytes:
        try:
            await asyncio.sleep(float(request))
        except asyncio.CancelledError:
            cancellations.append("Sleep")
            raise
        return request

    async def sleep_stream(request: bytes, context: object) -> Any:
        await asyncio.sleep(float(request))
        yield request

    async def boom(request: bytes, context: object) -> bytes:
        raise ValueError("the handler broke")

    async def denied(request: bytes, context: Any) -> bytes:
        await context.abort(grpc.StatusCode.PERMISSION_DENIED, "no")
        raise AssertionError("abort does not return")

    async def upstream(request: bytes, context: object) -> bytes:
        record_upstream_status(int(request))
        return request

    return {
        "Echo": grpc.unary_unary_rpc_method_handler(echo),
        "EchoStream": grpc.unary_stream_rpc_method_handler(echo_stream),
        "Sleep": grpc.unary_unary_rpc_method_handler(sleep),
        "SleepStream": grpc.unary_stream_rpc_method_handler(sleep_stream),
        "Boom": grpc.unary_unary_rpc_method_handler(boom),
        "Denied": grpc.unary_unary_rpc_method_handler(denied),
        "Upstream": grpc.unary_unary_rpc_method_handler(upstream),
    }


@asynccontextmanager
async def echo_server(
    *interceptors: Any, options: Sequence[tuple[str, Any]] = ()
) -> AsyncIterator[str]:
    """A running `grpc.aio` server hosting the stand-in service."""
    server = grpc.aio.server(interceptors=list(interceptors), options=list(options))
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler(ECHO_SERVICE, echo_handlers()),)
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await server.stop(None)


def mint_listener_certs(directory: Path, *, host: str = "127.0.0.1") -> BridgeCerts:
    """A CA and a leaf the Gateway can present to its own gRPC clients.

    Unlike the Bridge's certificate this one carries a subjectAltName, because
    a modern client verifies the name it dialled and every client here dials
    an address.
    """
    directory.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(tz=dt.UTC)

    ca_key = _key()
    ca_subject = _name("hue-grpc test ca")
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = _key()
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(_name(host))
        .issuer_name(ca_subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(host))]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_file = directory / "ca.pem"
    cert_file = directory / "listener.pem"
    key_file = directory / "listener.key"
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
        common_name=host, ca_file=ca_file, cert_file=cert_file, key_file=key_file
    )


@pytest.fixture
def listener_certs(tmp_path: Path) -> BridgeCerts:
    return mint_listener_certs(tmp_path / "listener")
