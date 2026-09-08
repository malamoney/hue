"""Command-line entry point: the binary the systemd unit invokes.

Every setting is a flag with a safe default, and the defaults are a loopback
listener with no TLS and no Gateway Token. Moving the Gateway onto the LAN is
three flags and no code — `hue_grpc.serving.config` is where the rules about
which combinations are allowed live.

The Gateway Token is the one thing that is never a flag *value*: `ps` shows
every argument a process was started with to every user on the host, so it is
read from a file, which under systemd is a credential.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

from hue_grpc import __version__
from hue_grpc.logs import configure_logging
from hue_grpc.serving.config import (
    DEFAULT_ADDRESS,
    DEFAULT_DEADLINE_SECONDS,
    DEFAULT_MAX_INBOUND_MESSAGE_BYTES,
    DEFAULT_MAX_OUTBOUND_MESSAGE_BYTES,
    DEFAULT_PORT,
    DEFAULT_SHUTDOWN_DRAIN_SECONDS,
    DEFAULT_SHUTDOWN_GRACE_SECONDS,
    GatewayConfig,
    TlsConfig,
    read_gateway_token,
)
from hue_grpc.serving.serve import serve

_log = logging.getLogger(__name__)

_REFLECTION = {"auto": None, "on": True, "off": False}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hue-grpc-server",
        description="Expose a subset of the Philips Hue CLIP v2 API over gRPC.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"hue-grpc-server {__version__}",
    )
    parser.add_argument(
        "--listen-address",
        default=DEFAULT_ADDRESS,
        metavar="IP",
        help="IP address to listen on (default: %(default)s). Anything but "
        "loopback also needs TLS and a gateway token.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="port to listen on, or 0 to let the OS choose (default: %(default)s)",
    )
    parser.add_argument(
        "--tls-certificate-file",
        type=Path,
        metavar="PATH",
        help="PEM certificate chain to present to gRPC clients",
    )
    parser.add_argument(
        "--tls-private-key-file",
        type=Path,
        metavar="PATH",
        help="PEM private key for the certificate chain",
    )
    parser.add_argument(
        "--gateway-token-file",
        type=Path,
        metavar="PATH",
        help="file holding the bearer token clients must present. A file, "
        "never a flag value: ps shows arguments to every user on the host.",
    )
    parser.add_argument(
        "--reflection",
        choices=sorted(_REFLECTION),
        default="auto",
        help="serve gRPC reflection (default: %(default)s, meaning on for a "
        "loopback listener and off for any other)",
    )
    parser.add_argument(
        "--default-deadline",
        type=float,
        default=DEFAULT_DEADLINE_SECONDS,
        metavar="SECONDS",
        help="deadline applied to unary calls that arrive without one "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--shutdown-drain",
        type=float,
        default=DEFAULT_SHUTDOWN_DRAIN_SECONDS,
        metavar="SECONDS",
        help="how long the listener keeps accepting after health starts "
        "reporting NOT_SERVING (default: %(default)s)",
    )
    parser.add_argument(
        "--shutdown-grace",
        type=float,
        default=DEFAULT_SHUTDOWN_GRACE_SECONDS,
        metavar="SECONDS",
        help="how long in-flight calls have to finish on shutdown before they "
        "are cancelled (default: %(default)s)",
    )
    parser.add_argument(
        "--max-inbound-message-bytes",
        type=int,
        default=DEFAULT_MAX_INBOUND_MESSAGE_BYTES,
        metavar="BYTES",
        help="largest request accepted (default: %(default)s)",
    )
    parser.add_argument(
        "--max-outbound-message-bytes",
        type=int,
        default=DEFAULT_MAX_OUTBOUND_MESSAGE_BYTES,
        metavar="BYTES",
        help="largest response sent (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default="INFO",
        type=str.upper,
        help="root log level (default: %(default)s)",
    )
    parser.add_argument(
        "--log-format",
        choices=("json", "text"),
        default="json",
        help="json for the journal, text for a terminal (default: %(default)s)",
    )
    return parser


def config_from(args: argparse.Namespace) -> GatewayConfig:
    """The listener `args` describes, or `ValueError` saying what is wrong."""
    certificate = args.tls_certificate_file
    private_key = args.tls_private_key_file
    if (certificate is None) != (private_key is None):
        raise ValueError(
            "--tls-certificate-file and --tls-private-key-file go together"
        )
    tls = (
        None
        if certificate is None
        else TlsConfig(certificate_file=certificate, private_key_file=private_key)
    )
    token = (
        None
        if args.gateway_token_file is None
        else read_gateway_token(args.gateway_token_file)
    )
    return GatewayConfig(
        address=args.listen_address,
        port=args.port,
        tls=tls,
        gateway_token=token,
        reflection=_REFLECTION[args.reflection],
        max_inbound_message_bytes=args.max_inbound_message_bytes,
        max_outbound_message_bytes=args.max_outbound_message_bytes,
        default_deadline=args.default_deadline,
        shutdown_drain=args.shutdown_drain,
        shutdown_grace=args.shutdown_grace,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(level=args.log_level, style=args.log_format)
    try:
        config = config_from(args)
    except (OSError, ValueError) as unusable:
        # Exit 2 and a one-line message, not a traceback: a unit that will
        # never start should say why in the first line of its journal.
        parser.error(str(unusable))
    try:
        asyncio.run(serve(config))
    except OSError as unavailable:
        _log.error("gateway could not start: %s", unavailable)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
