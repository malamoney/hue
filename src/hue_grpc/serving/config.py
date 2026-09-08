"""What the listener is, and what it refuses to be.

The defaults are the security posture: loopback, TLS off, no Gateway Token.
Everything the Gateway needs in order to listen on the LAN instead is a value
in here — a LAN bind with TLS and a token is a configuration change and
nothing more, because where the client ends up running is still undecided.

Two rules are enforced rather than documented, because getting them wrong
exposes lighting control to the network:

* A listener beyond loopback needs both TLS and a Gateway Token. There is no
  override. A TLS-terminating proxy on the same host talks to the loopback
  listener, so the case an override would serve does not arise.
* Reflection follows the listener unless it is asked for by name. Reflection
  on a LAN listener publishes the whole service surface to anyone who can
  reach it. On loopback it stays on even in production, deliberately: a caller
  that can reach a loopback listener can already call every RPC on it, so
  reflection tells it nothing it could not have found out, and it is how
  anyone debugging the running service finds their way around.
"""

from __future__ import annotations

import ipaddress
import logging
import stat
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_ADDRESS",
    "DEFAULT_DEADLINE_SECONDS",
    "DEFAULT_MAX_INBOUND_MESSAGE_BYTES",
    "DEFAULT_MAX_OUTBOUND_MESSAGE_BYTES",
    "DEFAULT_PORT",
    "DEFAULT_SHUTDOWN_DRAIN_SECONDS",
    "DEFAULT_SHUTDOWN_GRACE_SECONDS",
    "GatewayConfig",
    "TlsConfig",
    "read_gateway_token",
]

#: Loopback. Anything else is a deliberate act with two more settings attached.
DEFAULT_ADDRESS = "127.0.0.1"

#: The port the plan's NixOS module offers as its default.
DEFAULT_PORT = 50051

#: A `LightPut` is a few hundred bytes. A megabyte is already absurdly
#: generous, and refusing more costs a misbehaving client nothing it deserves.
DEFAULT_MAX_INBOUND_MESSAGE_BYTES = 1 * 1024 * 1024

#: Responses are larger — a full light list, or reflection handing over the
#: file descriptors for every service — but still nowhere near this.
DEFAULT_MAX_OUTBOUND_MESSAGE_BYTES = 4 * 1024 * 1024

#: Applied to unary calls that arrive without a deadline of their own, so that
#: a client which forgot one cannot pin an upstream request open forever.
DEFAULT_DEADLINE_SECONDS = 10.0

#: How long in-flight calls have to finish once shutdown starts. Past it they
#: are cancelled, and cancellation is what unwinds upstream Bridge work.
DEFAULT_SHUTDOWN_GRACE_SECONDS = 5.0

#: How long the listener keeps accepting after health says `NOT_SERVING`.
#: Without a pause the two happen in the same breath and saying so first buys
#: nothing: a client polling health learns the gateway is going away only by
#: failing to connect. Short, because the only thing it costs is shutdown.
DEFAULT_SHUTDOWN_DRAIN_SECONDS = 0.5

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TlsConfig:
    """The certificate the Gateway presents to its clients.

    Not the Bridge's certificate, and not anything to do with verifying one:
    that lives in `hue_grpc.hue.tls`. These are paths rather than bytes so the
    private key can be a systemd credential that only exists at runtime.
    """

    certificate_file: Path
    private_key_file: Path

    def __post_init__(self) -> None:
        if not self.certificate_file.is_file():
            raise ValueError(f"tls certificate {self.certificate_file} is not a file")
        if not self.private_key_file.is_file():
            raise ValueError(f"tls private key {self.private_key_file} is not a file")

    def read(self) -> tuple[bytes, bytes]:
        """The certificate chain and private key, in that order."""
        return (
            self.certificate_file.read_bytes(),
            self.private_key_file.read_bytes(),
        )


@dataclass(frozen=True, repr=False)
class GatewayConfig:
    """Everything the listener needs, with safe defaults for all of it."""

    #: An IP literal, never a hostname: every rule below turns on whether this
    #: is loopback, and a name that resolves elsewhere would answer that
    #: question somewhere other than the configuration.
    address: str = DEFAULT_ADDRESS
    #: Port 0 asks the operating system for a free one, which is how the
    #: tests bind without fighting over a number.
    port: int = DEFAULT_PORT
    #: `None` means plaintext, which is only allowed on loopback.
    tls: TlsConfig | None = None
    #: The bearer token a client must present. `None` leaves the auth
    #: interceptor in the chain doing nothing, rather than out of it.
    gateway_token: str | None = None
    #: `None` follows the listener; `True` and `False` say so outright.
    reflection: bool | None = None
    max_inbound_message_bytes: int = DEFAULT_MAX_INBOUND_MESSAGE_BYTES
    max_outbound_message_bytes: int = DEFAULT_MAX_OUTBOUND_MESSAGE_BYTES
    default_deadline: float = DEFAULT_DEADLINE_SECONDS
    shutdown_drain: float = DEFAULT_SHUTDOWN_DRAIN_SECONDS
    shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE_SECONDS

    def __post_init__(self) -> None:
        try:
            ipaddress.ip_address(self.address)
        except ValueError as not_an_address:
            raise ValueError(
                f"listen address {self.address!r} is not an IP address; a name "
                "like 'localhost' resolves at bind time, and whether the "
                "listener is loopback has to be readable from the config"
            ) from not_an_address
        if not 0 <= self.port <= 65535:
            raise ValueError(f"port {self.port} is not a port number")
        if not self.is_loopback:
            if self.tls is None:
                raise ValueError(
                    f"listening on {self.address} needs tls: a plaintext "
                    "listener beyond loopback carries the Gateway Token in "
                    "the clear"
                )
            if not self.gateway_token:
                raise ValueError(
                    f"listening on {self.address} needs a gateway token: "
                    "anything that can reach the port could otherwise control "
                    "the lights"
                )
        if self.gateway_token is not None and not self.gateway_token:
            raise ValueError("gateway token is empty; leave it unset instead")
        if self.max_inbound_message_bytes < 1:
            raise ValueError("inbound message limit must be positive")
        if self.max_outbound_message_bytes < 1:
            raise ValueError("outbound message limit must be positive")
        if self.default_deadline <= 0:
            raise ValueError("default deadline must be positive")
        if self.shutdown_drain < 0:
            raise ValueError("shutdown drain cannot be negative")
        if self.shutdown_grace < 0:
            raise ValueError("shutdown grace cannot be negative")

    @property
    def is_loopback(self) -> bool:
        return ipaddress.ip_address(self.address).is_loopback

    @property
    def reflection_enabled(self) -> bool:
        return self.is_loopback if self.reflection is None else self.reflection

    @property
    def listen_target(self) -> str:
        """The `host:port` gRPC binds, bracketing IPv6 as the format needs."""
        if ipaddress.ip_address(self.address).version == 6:
            return f"[{self.address}]:{self.port}"
        return f"{self.address}:{self.port}"

    def __repr__(self) -> str:
        """Everything but the token: a config travels through tracebacks."""
        token = "<redacted>" if self.gateway_token is not None else None
        return (
            f"GatewayConfig(address={self.address!r}, port={self.port!r}, "
            f"tls={self.tls!r}, gateway_token={token}, "
            f"reflection={self.reflection!r}, "
            f"max_inbound_message_bytes={self.max_inbound_message_bytes!r}, "
            f"max_outbound_message_bytes={self.max_outbound_message_bytes!r}, "
            f"default_deadline={self.default_deadline!r}, "
            f"shutdown_drain={self.shutdown_drain!r}, "
            f"shutdown_grace={self.shutdown_grace!r})"
        )


def read_gateway_token(path: Path) -> str:
    """The Gateway Token out of `path`, which is where secrets come from.

    Never a command-line argument: `ps` shows every argument a service was
    started with, to every user on the box. Under systemd this path is a
    credential, which exists only for this unit and only while it runs.

    Trailing whitespace goes, because a token file written by hand ends with
    a newline and that newline is not part of the token.
    """
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"gateway token file {path} is empty")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        _log.warning(
            "gateway token file %s is mode %04o; anyone on this host can read it",
            path,
            mode,
        )
    return token
