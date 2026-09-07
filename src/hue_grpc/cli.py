"""Command-line entry point.

This is the binary the systemd unit invokes. The server itself is not built
yet; until then the entry point exists so that packaging, the console script
and the unit file can all be exercised end to end.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from hue_grpc import __version__


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    print(
        "hue-grpc-server: no gRPC server yet, see issue #9 (server bootstrap).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
