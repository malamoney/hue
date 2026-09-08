"""``python -m fake_hue`` — run the fake Bridge, or mint its certificate.

``serve`` (the default) starts the HTTPS server. ``mint-certs`` writes a
CA/leaf pair to a directory and exits, so a build step can mint once and hand
the CA to the Gateway and the leaf to a later ``serve`` on another host.
"""

from __future__ import annotations

import argparse
import signal
import threading
from collections.abc import Sequence
from pathlib import Path

from fake_hue.bridge import FakeHueBridge
from fake_hue.certs import BridgeCerts, mint_bridge_certs


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fake_hue", description=__doc__)
    commands = parser.add_subparsers(dest="command")

    mint = commands.add_parser("mint-certs", help="write a CA/leaf pair and exit")
    mint.add_argument("--bridge-id", required=True)
    mint.add_argument("--out", type=Path, required=True, metavar="DIR")

    parser.add_argument("--bridge-id", metavar="ID")
    parser.add_argument("--listen-address", default="0.0.0.0", metavar="IP")
    parser.add_argument(
        "--port",
        type=int,
        default=443,
        help="port to listen on (default: 443, where a real Bridge serves)",
    )
    parser.add_argument("--application-key", metavar="KEY")
    parser.add_argument(
        "--application-key-file",
        type=Path,
        metavar="PATH",
        help="file holding the expected Application Key, for parity with how "
        "the Gateway is given its copy",
    )
    parser.add_argument(
        "--cert-dir",
        type=Path,
        metavar="DIR",
        help="directory holding ca.pem/bridge.pem/bridge.key, or where to "
        "mint them if it does not already have them",
    )
    parser.add_argument(
        "--log", action="store_true", help="log every request to stderr"
    )
    return parser


def _certs(directory: Path, *, bridge_id: str) -> BridgeCerts:
    existing = BridgeCerts(
        common_name=bridge_id.lower(),
        ca_file=directory / "ca.pem",
        cert_file=directory / "bridge.pem",
        key_file=directory / "bridge.key",
    )
    have = (existing.ca_file, existing.cert_file, existing.key_file)
    if all(path.is_file() for path in have):
        return existing
    return mint_bridge_certs(directory, bridge_id=bridge_id)


def _application_key(args: argparse.Namespace) -> str:
    key: str
    if args.application_key_file is not None:
        path: Path = args.application_key_file
        key = path.read_text(encoding="utf-8").strip()
    elif args.application_key is not None:
        key = str(args.application_key)
    else:
        raise SystemExit(
            "fake_hue: an Application Key is required "
            "(--application-key or --application-key-file)"
        )
    if not key:
        raise SystemExit("fake_hue: the Application Key is empty")
    return key


def _serve(args: argparse.Namespace) -> int:
    if not args.bridge_id:
        raise SystemExit("fake_hue: --bridge-id is required")
    application_key = _application_key(args)
    directory = args.cert_dir or Path("fake-hue-certs")
    bridge = FakeHueBridge(
        _certs(directory, bridge_id=args.bridge_id),
        application_key=application_key,
        host=args.listen_address,
        port=args.port,
        log=args.log,
    )
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: done.set())
    signal.signal(signal.SIGINT, lambda _signum, _frame: done.set())
    with bridge:
        print(f"fake hue bridge on {bridge.address}", flush=True)
        done.wait()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "mint-certs":
        certs = mint_bridge_certs(args.out, bridge_id=args.bridge_id)
        print(certs.ca_file)
        return 0
    return _serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
