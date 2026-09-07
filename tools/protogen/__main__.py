"""Command-line entry point for the generator.

    python -m protogen --spec openapi.yaml --package hue.v1 \
        --out proto/hue/v1/lighting.proto --root LightGet --root LightPut
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from protogen.convert import convert_document
from protogen.numbering import FieldNumbers
from protogen.spec import load_spec

GENERATED_HEADER = """Generated from the Hue OpenAPI document. Do not edit.

Regenerate with `python -m protogen`. See
docs/adr/0001-custom-openapi-to-proto-generator.md."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="protogen")
    parser.add_argument("--spec", type=Path, required=True, help="OpenAPI document")
    parser.add_argument("--out", type=Path, required=True, help="output .proto path")
    parser.add_argument("--package", default="hue.v1", help="protobuf package")
    parser.add_argument(
        "--numbers",
        type=Path,
        help=(
            "field number lock file, read then updated. Without it, numbers "
            "follow spec order and shift whenever upstream inserts a property."
        ),
    )
    parser.add_argument(
        "--root",
        dest="roots",
        action="append",
        required=True,
        metavar="SCHEMA",
        help="schema to emit; may be repeated. Referenced schemas follow.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    numbers = FieldNumbers.load(args.numbers) if args.numbers else FieldNumbers()

    proto = convert_document(
        load_spec(args.spec),
        args.roots,
        package=args.package,
        header=GENERATED_HEADER,
        numbers=numbers,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(proto.render(), encoding="utf-8")
    if args.numbers:
        numbers.save(args.numbers)

    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
