"""The command-line entry point, which is what the systemd unit invokes.

Two things matter here. Every flag has to land on the right field of the
config, because the NixOS module in issue #13 will drive the service entirely
through them. And a listener that cannot legally exist has to be refused with
a line, not a traceback: a unit that will never start should say why in the
first line of its journal.
"""

from __future__ import annotations

import importlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import grpc
import pytest
from conftest import BridgeCerts, run
from grpc_health.v1 import health_pb2, health_pb2_grpc

from hue_grpc import __version__
from hue_grpc.cli import build_parser, config_from, main
from hue_grpc.serving.config import GatewayConfig

PYPROJECT = Path(__file__).parents[2] / "pyproject.toml"


def config(*argv: str) -> GatewayConfig:
    return config_from(build_parser().parse_args(argv))


def test_version_flag_reports_the_package_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_console_script_target_resolves() -> None:
    """A typo in pyproject's script target would ship a broken binary."""
    scripts = tomllib.loads(PYPROJECT.read_text())["project"]["scripts"]
    module_name, _, attribute = scripts["hue-grpc-server"].partition(":")

    entry_point = getattr(importlib.import_module(module_name), attribute)

    assert callable(entry_point)


def test_no_arguments_is_a_loopback_listener_with_nothing_switched_on() -> None:
    default = config()

    assert default.address == "127.0.0.1"
    assert default.port == 50051
    assert default.tls is None
    assert default.gateway_token is None
    assert default.reflection_enabled


def test_the_lan_flip_is_three_flags(
    listener_certs: BridgeCerts, tmp_path: Path
) -> None:
    token_file = tmp_path / "gateway-token"
    token_file.write_text("a-gateway-token\n")

    lan = config(
        "--listen-address",
        "192.168.86.10",
        "--tls-certificate-file",
        str(listener_certs.cert_file),
        "--tls-private-key-file",
        str(listener_certs.key_file),
        "--gateway-token-file",
        str(token_file),
    )

    assert lan.address == "192.168.86.10"
    assert lan.tls is not None
    assert lan.gateway_token == "a-gateway-token"
    assert not lan.reflection_enabled


def test_reflection_can_be_forced_either_way() -> None:
    assert config("--reflection", "off").reflection_enabled is False
    assert config("--reflection", "on").reflection_enabled is True


def test_half_a_keypair_is_refused_before_anything_binds(
    listener_certs: BridgeCerts,
) -> None:
    with pytest.raises(ValueError, match="go together"):
        config("--tls-certificate-file", str(listener_certs.cert_file))


def test_a_listener_that_cannot_legally_exist_exits_two_with_a_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--listen-address", "0.0.0.0"])

    assert exit_info.value.code == 2
    printed = capsys.readouterr().err
    assert "needs tls" in printed
    assert "Traceback" not in printed


def test_a_missing_token_file_is_a_line_too(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--gateway-token-file", str(tmp_path / "absent")])

    assert exit_info.value.code == 2
    assert "absent" in capsys.readouterr().err


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_the_binary_serves_health_and_stops_on_sigterm(tmp_path: Path) -> None:
    """The whole thing, as a process: started the way the unit starts it,
    answering the way a supervisor asks, stopped the way systemd stops it."""
    port = free_port()
    log_file = tmp_path / "gateway.log"
    with log_file.open("wb") as log:
        gateway = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from hue_grpc.cli import main; raise SystemExit(main())",
                "--port",
                str(port),
                "--log-format",
                "json",
            ],
            stdout=log,
            stderr=log,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    try:
        listening = await_listening_line(log_file, gateway)
        assert listening["port"] == port

        async def scenario() -> int:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                stub = health_pb2_grpc.HealthStub(channel)
                response = await stub.Check(health_pb2.HealthCheckRequest())
                return int(response.status)

        assert run(scenario()) == health_pb2.HealthCheckResponse.SERVING

        gateway.send_signal(signal.SIGTERM)
        assert gateway.wait(timeout=30) == 0
    finally:
        if gateway.poll() is None:  # pragma: no cover - only on a failure
            gateway.kill()
            gateway.wait(timeout=30)

    assert "gateway stopped" in log_file.read_text()


def await_listening_line(
    log_file: Path, gateway: subprocess.Popen[bytes], timeout: float = 30.0
) -> dict[str, object]:
    """The structured record the gateway writes once it is listening."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in log_file.read_text().splitlines():
            if '"gateway listening"' in line:
                record = json.loads(line)
                assert isinstance(record, dict)
                return record
        if gateway.poll() is not None:
            raise AssertionError(
                f"gateway exited with {gateway.returncode}: {log_file.read_text()}"
            )
        time.sleep(0.05)
    raise AssertionError(f"gateway never reported listening: {log_file.read_text()}")
