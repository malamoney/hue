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
from protogen.layout import generate_files, load_manifest
from protogen.numbering import FieldNumbers
from protogen.spec import load_spec

GENERATED_HEADER = """Generated from the Hue OpenAPI document. Do not edit.

Regenerate with `python -m protogen`. See
docs/adr/0001-custom-openapi-to-proto-generator.md."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="protogen")
    parser.add_argument(
        "--manifest",
        type=Path,
        help="generate every file described by this manifest (see proto/manifest.toml)",
    )
    parser.add_argument("--spec", type=Path, help="OpenAPI document")
    parser.add_argument("--out", type=Path, help="output .proto path")
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
        "--allow-empty",
        dest="allow_empty",
        action="append",
        metavar="SCOPE",
        help="scope permitted to produce a message with no fields",
    )
    parser.add_argument(
        "--root",
        dest="roots",
        action="append",
        metavar="SCHEMA",
        help="schema to emit; may be repeated. Referenced schemas follow.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.manifest:
        conflicting = [
            name
            for name, value in (
                ("--spec", args.spec),
                ("--out", args.out),
                ("--root", args.roots),
                ("--numbers", args.numbers),
            )
            if value
        ]
        if conflicting:
            unwanted = ", ".join(conflicting)
            parser.error(
                f"--manifest reads everything from the TOML; remove {unwanted}"
            )
        return _generate_from_manifest(args.manifest)

    if not (args.spec and args.out and args.roots):
        parser.error("--spec, --out and --root are required without --manifest")

    numbers = FieldNumbers.load(args.numbers) if args.numbers else FieldNumbers()

    proto = convert_document(
        load_spec(args.spec),
        args.roots,
        package=args.package,
        header=GENERATED_HEADER,
        numbers=numbers,
        allow_empty=args.allow_empty,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(proto.render(), encoding="utf-8")
    if args.numbers:
        numbers.save(args.numbers)

    print(f"wrote {args.out}", file=sys.stderr)
    return 0


def _generate_from_manifest(path: Path) -> int:
    """Generate from a manifest.

    Paths inside the manifest are relative to the working directory, not to
    the manifest itself, so the generator is always run from the repo root.
    """
    manifest = load_manifest(path)
    numbers_path = Path(manifest.numbers)

    numbers = FieldNumbers.load(numbers_path)
    generated = generate_files(
        load_spec(Path(manifest.spec)),
        manifest.files,
        package=manifest.package,
        default_file=manifest.default_file,
        numbers=numbers,
        header=GENERATED_HEADER,
        allow_empty=manifest.allow_empty,
    )

    for relative, proto in sorted(generated.items()):
        out = Path(manifest.out_dir) / relative
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(proto.render(), encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)

    numbers.save(numbers_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
