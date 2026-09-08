"""Command-line entry point: the binary the systemd unit invokes.

Every setting is a flag with a safe default, and the defaults are a loopback
listener with no TLS and no Gateway Token. Moving the Gateway onto the LAN is
three flags and no code — `hue_grpc.serving.config` is where the rules about
which combinations are allowed live.

The Gateway Token is the one thing that is never a flag *value*: `ps` shows
every argument a process was started with to every user on the host, so it is
read from a file, which under systemd is a credential.

Two things can be asked of the binary. With no subcommand it serves, which is
what the unit does. `pair` mints an Application Key from a Bridge whose link
button has just been pressed and writes it into the Registry — a thing done
once, by a person standing next to the Bridge, and the only way an entry gets
there for the server to find on its next start.

Neither takes a path to the Registry: it is `$STATE_DIRECTORY` under systemd
and `$XDG_STATE_HOME/hue-grpc` outside it, so the two commands cannot be
pointed at different files by mistake.

A declaratively-installed Gateway does not pair. `--bridge-address`,
`--bridge-id` and `--credentials-file` describe one Bridge outright — the
first two are configuration, the third a Credentials File holding the
Application Key — and when they are set the Registry file is not read at all.
`hue_grpc.static_registry` is where that path lives.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import socket
from collections.abc import Sequence
from pathlib import Path

from hue_grpc import __version__
from hue_grpc.events.fanout import DEFAULT_QUEUE_SIZE, EventFanout
from hue_grpc.events.service import hosted_event_service
from hue_grpc.hue import pairing
from hue_grpc.hue.events import BridgeEvents
from hue_grpc.hue.lights import Lights
from hue_grpc.hue.transport import HueTransport, HueTransportError
from hue_grpc.lighting.service import hosted_lighting_service
from hue_grpc.logs import configure_logging
from hue_grpc.registry import (
    Registry,
    RegistryEntry,
    RegistryError,
    default_registry_path,
)
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
from hue_grpc.static_registry import (
    CredentialsFileError,
    load_bridge_credentials,
    static_entry,
)

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
        "--bridge-ca-file",
        type=Path,
        metavar="PATH",
        help="PEM CA to verify the bridge's certificate against, instead of "
        "the vendored Philips root. The common-name check still runs; this "
        "only swaps the trust anchor, for a bridge behind a certificate this "
        "gateway was not shipped knowing about.",
    )
    static = parser.add_argument_group(
        "static bridge",
        "Describe one bridge by configuration instead of pairing. "
        "--bridge-address turns this on; it then also needs --bridge-id and "
        "--credentials-file, and the registry file is left untouched.",
    )
    static.add_argument(
        "--bridge-address",
        metavar="IP",
        help="the bridge's address on the local network. Setting this selects "
        "static configuration over the registry.",
    )
    static.add_argument(
        "--bridge-id",
        metavar="ID",
        help="the bridge's id, e.g. ECB5FAFFFE334703, asserted against the "
        "certificate the bridge presents",
    )
    static.add_argument(
        "--credentials-file",
        type=Path,
        metavar="PATH",
        help="a Credentials File: key=value lines holding application-key and, "
        "optionally, client-key. A file, never a flag value; under systemd a "
        "LoadCredential path.",
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
        "--event-queue-size",
        type=int,
        default=DEFAULT_QUEUE_SIZE,
        metavar="EVENTS",
        help="how far behind one event subscriber may fall before it starts "
        "losing events, and being told so (default: %(default)s)",
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

    commands = parser.add_subparsers(dest="command", metavar="{pair}")
    pair_command = commands.add_parser(
        "pair",
        help="mint an application key from a bridge and register it",
        description="Pair with a bridge and write the result into the "
        "registry. Press the bridge's link button first: it has about thirty "
        "seconds of memory for it.",
    )
    pair_command.add_argument(
        "--bridge-address",
        required=True,
        metavar="IP",
        help="the bridge's address on the local network",
    )
    pair_command.add_argument(
        "--bridge-id",
        required=True,
        metavar="ID",
        help="the bridge's id, e.g. ECB5FAFFFE334703. Asserted against the "
        "certificate the bridge presents, so a wrong one is refused rather "
        "than paired with whatever answered.",
    )
    pair_command.add_argument(
        "--instance",
        default=default_instance(),
        help="which gateway this is, as it appears in the bridge's app list "
        "(default: %(default)s)",
    )
    pair_command.add_argument(
        "--bridge-ca-file",
        type=Path,
        metavar="PATH",
        help="PEM CA to verify the bridge's certificate against, instead of "
        "the vendored Philips root",
    )
    return parser


def default_instance() -> str:
    """This host's short name, which is what the Bridge's app list will show.

    Truncated rather than refused: the limit is the Bridge's, and a name too
    long for it is not a reason to make somebody pass a flag. `--instance`
    is there for anyone who wants to choose, and is checked in full.
    """
    name = socket.gethostname().split(".")[0] or "gateway"
    return name[: pairing.INSTANCE_NAME_LIMIT]


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


def _static_bridge_entry(args: argparse.Namespace) -> RegistryEntry | None:
    """The bridge `--bridge-address` and friends describe, or `None`.

    `None` means no static bridge was configured, so the Registry file is the
    source of truth. A `ValueError` or `CredentialsFileError` means one was
    configured but cannot be used — a half-given set of flags, or an
    unreadable Credentials File — and the caller turns that into a one-line
    exit rather than a traceback.
    """
    companions = (
        ("--bridge-id", args.bridge_id),
        ("--credentials-file", args.credentials_file),
    )
    if args.bridge_address is None:
        stray = [flag for flag, value in companions if value is not None]
        if stray:
            raise ValueError(
                f"{' and '.join(stray)} only applies with --bridge-address"
            )
        return None
    missing = [flag for flag, value in companions if value is None]
    if missing:
        raise ValueError(f"--bridge-address also needs {' and '.join(missing)}")
    return static_entry(
        bridge_id=args.bridge_id,
        address=args.bridge_address,
        credentials=load_bridge_credentials(args.credentials_file),
    )


def _bridge_ca_pem(path: Path | None) -> str | None:
    """The trust anchor for the Bridge connection, read from `path`.

    `None` — the default — leaves the vendored Philips `root-bridge` CA in
    place. A path replaces it, and only it: the Bridge's certificate is
    verified against this CA, and the common-name check in `hue_grpc.hue.tls`
    still runs on top. It is how a Bridge behind a certificate this Gateway
    was not shipped knowing about is reached without turning verification off.
    """
    if path is None:
        return None
    try:
        pem = path.read_text(encoding="utf-8")
    except OSError as unreadable:
        raise ValueError(
            f"could not read bridge CA file {path}: {unreadable}"
        ) from unreadable
    if "BEGIN CERTIFICATE" not in pem:
        raise ValueError(f"bridge CA file {path} holds no PEM certificate")
    return pem


async def _serve(
    config: GatewayConfig,
    entry: RegistryEntry | None,
    *,
    ca_pem: str | None = None,
    event_queue_size: int = DEFAULT_QUEUE_SIZE,
) -> None:
    """Listen, serving `entry`'s Bridge if there is one.

    An unpaired Gateway still listens and still hosts both services: health,
    reflection and a clear FAILED_PRECONDITION are more use than a unit that
    refuses to start, and the fix — walking to the Bridge — is not one
    anybody can perform from a failed boot.
    """
    if entry is None:
        _log.warning(
            "no bridge is registered; lighting and event calls will be "
            "refused until `hue-grpc-server pair` has run"
        )
        await serve(config, [hosted_lighting_service(None), hosted_event_service(None)])
        return
    transport = HueTransport(
        bridge_id=entry.bridge_id,
        address=entry.address,
        application_key=entry.application_key,
        ca_pem=ca_pem,
    )
    async with transport:
        # One transport for both services: a Bridge is one host, one
        # connection pool and one Application Key, whichever of its paths is
        # being asked for. The event stream holds a connection of its own out
        # of that pool for as long as the gateway is up.
        lights = Lights(transport)
        fanout = EventFanout(
            events=BridgeEvents(transport),
            lights=lights,
            queue_size=event_queue_size,
        )
        await serve(
            config,
            [
                hosted_lighting_service(lights),
                hosted_event_service(fanout, bridge_id=entry.bridge_id),
            ],
        )


async def _mint(
    *, address: str, bridge_id: str, instance: str, ca_pem: str | None = None
) -> pairing.PairedSecrets:
    """One Pairing exchange, over a connection verified as `bridge_id`."""
    async with HueTransport(
        bridge_id=bridge_id, address=address, ca_pem=ca_pem
    ) as transport:
        return await pairing.pair(transport, instance=instance)


def pair_with_bridge(args: argparse.Namespace) -> int:
    """The `pair` subcommand: mint an Application Key and write it down."""
    try:
        ca_pem = _bridge_ca_pem(args.bridge_ca_file)
    except ValueError as unusable:
        _log.error("%s", unusable)
        return 1
    registry = Registry(default_registry_path())
    try:
        registered = registry.load()
    except RegistryError as unreadable:
        _log.error("%s", unreadable)
        return 1
    if registered is not None:
        # Pairing again would mint a second key and abandon the first in the
        # bridge's app list, where only a person with the Hue app can remove
        # it. Following a bridge to a new address needs no new key at all.
        _log.error(
            "bridge %s is already registered in %s; remove that file to pair "
            "from scratch",
            registered.bridge_id,
            registry.path,
        )
        return 1
    try:
        secrets = asyncio.run(
            _mint(
                address=args.bridge_address,
                bridge_id=args.bridge_id,
                instance=args.instance,
                ca_pem=ca_pem,
            )
        )
    except pairing.LinkButtonNotPressedError:
        _log.error(
            "the bridge's link button has not been pressed; press it and run "
            "this again within thirty seconds"
        )
        return 1
    except (pairing.PairingError, HueTransportError, ValueError) as failed:
        _log.error("pairing failed: %s", failed)
        return 1
    registry.save(
        RegistryEntry(
            bridge_id=args.bridge_id,
            address=args.bridge_address,
            model=None,
            firmware=None,
            last_contact=dt.datetime.now(tz=dt.UTC),
            application_key=secrets.application_key,
            client_key=secrets.client_key,
        )
    )
    _log.info(
        "bridge %s registered; start the gateway to serve its lights",
        args.bridge_id,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(level=args.log_level, style=args.log_format)
    if args.command == "pair":
        return pair_with_bridge(args)
    try:
        config = config_from(args)
        static = _static_bridge_entry(args)
        ca_pem = _bridge_ca_pem(args.bridge_ca_file)
    except (OSError, ValueError, CredentialsFileError) as unusable:
        # Exit 2 and a one-line message, not a traceback: a unit that will
        # never start should say why in the first line of its journal.
        parser.error(str(unusable))
    if static is not None:
        _log.info(
            "serving statically configured bridge %s at %s; registry file "
            "not consulted",
            static.bridge_id,
            static.address,
        )
        entry: RegistryEntry | None = static
    else:
        try:
            entry = Registry(default_registry_path()).load()
        except RegistryError as unreadable:
            # Never treated as "nothing is registered": that would send the
            # gateway off to pair again and strand a working application key.
            _log.error("gateway could not start: %s", unreadable)
            return 1
    try:
        asyncio.run(
            _serve(
                config,
                entry,
                ca_pem=ca_pem,
                event_queue_size=args.event_queue_size,
            )
        )
    except OSError as unavailable:
        _log.error("gateway could not start: %s", unavailable)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
