"""Guards against the Nix package and the Python package drifting apart."""

from __future__ import annotations

import re
from pathlib import Path

from hue_grpc import __version__

PACKAGE_NIX = Path(__file__).parents[2] / "nix" / "package.nix"


def test_nix_package_version_matches_python_version() -> None:
    """nix/package.nix restates the version; nothing else keeps them aligned."""
    match = re.search(
        r'^\s*version = "([^"]+)";', PACKAGE_NIX.read_text(), re.MULTILINE
    )

    assert match is not None, "no version attribute found in nix/package.nix"
    assert match.group(1) == __version__
