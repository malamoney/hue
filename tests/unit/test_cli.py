"""Tests for the command-line entry point.

The gateway itself lands in later issues; what matters here is that the
packaged console script actually resolves and runs, since that is what the
systemd unit will invoke.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

from hue_grpc import __version__
from hue_grpc.cli import main

PYPROJECT = Path(__file__).parents[2] / "pyproject.toml"


def test_version_flag_reports_the_package_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_main_exits_cleanly_with_no_arguments() -> None:
    assert main([]) == 0


def test_console_script_target_resolves() -> None:
    """A typo in pyproject's script target would ship a broken binary."""
    scripts = tomllib.loads(PYPROJECT.read_text())["project"]["scripts"]
    module_name, _, attribute = scripts["hue-grpc-server"].partition(":")

    entry_point = getattr(importlib.import_module(module_name), attribute)

    assert callable(entry_point)
