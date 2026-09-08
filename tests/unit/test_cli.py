"""The command-line entry point, which is what the systemd unit invokes.

Three things matter here. Every flag has to land on the right field of the
config, because the NixOS module in issue #13 will drive the service entirely
through them. A listener that cannot legally exist has to be refused with a
line, not a traceback: a unit that will never start should say why in the
first line of its journal. And the Registry is read on the way up and written
by `pair`, so what those two do to the file is what decides whether a walk to
the bridge has to happen twice.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import signal
import socket
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import grpc
import pytest
from conftest import BRIDGE_ID, BridgeCerts, run
from grpc_health.v1 import health_pb2, health_pb2_grpc

from hue.v1 import lighting_service_pb2
from hue.v1 import lighting_service_pb2_grpc as lighting_grpc
from hue_grpc import __version__, cli
from hue_grpc.cli import build_parser, config_from, default_instance, main
from hue_grpc.events.service import SERVICE_NAME as EVENT_SERVICE
from hue_grpc.hue.pairing import (
    INSTANCE_NAME_LIMIT,
    LinkButtonNotPressedError,
    PairedSecrets,
    device_type,
)
from hue_grpc.lighting.service import SERVICE_NAME
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


# Pairing, and the registry the server reads on its way up


@pytest.fixture
def state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A registry of this test's own, wherever the gateway would look."""
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path / "hue-grpc" / "registry.json"


def registered(state_home: Path) -> dict[str, object]:
    bridge = json.loads(state_home.read_text())["bridge"]
    assert isinstance(bridge, dict)
    return bridge


def test_pairing_needs_to_know_which_bridge() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["pair", "--bridge-address", "192.168.86.223"])

    assert exit_info.value.code == 2


def test_the_instance_name_fits_the_bridge_s_limit() -> None:
    assert len(default_instance()) <= INSTANCE_NAME_LIMIT
    assert device_type(default_instance())


def test_pairing_writes_the_minted_key_into_the_registry(
    monkeypatch: pytest.MonkeyPatch, state_home: Path
) -> None:
    async def mint(
        *, address: str, bridge_id: str, instance: str, ca_pem: str | None = None
    ) -> PairedSecrets:
        assert (address, bridge_id) == ("192.168.86.223", BRIDGE_ID)
        return PairedSecrets(application_key="minted", client_key="also-minted")

    monkeypatch.setattr(cli, "_mint", mint)

    code = main(
        ["pair", "--bridge-address", "192.168.86.223", "--bridge-id", BRIDGE_ID]
    )

    assert code == 0
    assert registered(state_home)["application_key"] == "minted"
    assert registered(state_home)["id"] == BRIDGE_ID
    # Owner-only: the file holds the application key in the clear.
    assert stat.S_IMODE(state_home.stat().st_mode) == 0o600


def test_pairing_twice_would_strand_a_working_key_and_is_refused(
    monkeypatch: pytest.MonkeyPatch, state_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def mint(
        *, address: str, bridge_id: str, instance: str, ca_pem: str | None = None
    ) -> PairedSecrets:
        raise AssertionError("the bridge should not have been asked")

    monkeypatch.setattr(cli, "_mint", mint)
    state_home.parent.mkdir(parents=True)
    state_home.write_text(
        json.dumps(
            {
                "version": 1,
                "bridge": {
                    "id": BRIDGE_ID,
                    "address": "192.168.86.223",
                    "model": None,
                    "firmware": None,
                    "last_contact": None,
                    "application_key": "the-first-key",
                    "client_key": None,
                },
            }
        )
    )

    with caplog.at_level(logging.ERROR):
        code = main(
            ["pair", "--bridge-address", "192.168.86.223", "--bridge-id", BRIDGE_ID]
        )

    assert code == 1
    assert "already registered" in caplog.text
    assert registered(state_home)["application_key"] == "the-first-key"


def test_an_unpressed_link_button_says_what_to_do_about_it(
    monkeypatch: pytest.MonkeyPatch, state_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def mint(
        *, address: str, bridge_id: str, instance: str, ca_pem: str | None = None
    ) -> PairedSecrets:
        raise LinkButtonNotPressedError(101, "link button not pressed")

    monkeypatch.setattr(cli, "_mint", mint)

    with caplog.at_level(logging.ERROR):
        code = main(
            ["pair", "--bridge-address", "192.168.86.223", "--bridge-id", BRIDGE_ID]
        )

    assert code == 1
    assert "link button" in caplog.text
    assert not state_home.exists()


def test_a_registry_it_cannot_read_stops_the_gateway_starting(
    state_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Never "nothing is registered": that pairs again and orphans a key."""
    state_home.parent.mkdir(parents=True)
    state_home.write_text("{ this is not a registry")

    with caplog.at_level(logging.ERROR):
        code = main(["--port", "0"])

    assert code == 1
    assert "could not start" in caplog.text


def test_the_binary_serves_the_registered_bridge(state_home: Path) -> None:
    """The wiring, as a process: registry entry in, lighting calls out.

    The bridge is a port nothing listens on, so the call fails — but it fails
    as UNAVAILABLE, which is only reachable if the entry became a transport
    and the transport became this service.
    """
    state_home.parent.mkdir(parents=True)
    state_home.write_text(
        json.dumps(
            {
                "version": 1,
                "bridge": {
                    "id": BRIDGE_ID,
                    # Nothing listens on port 1, and nothing is meant to.
                    "address": "127.0.0.1:1",
                    "model": None,
                    "firmware": None,
                    "last_contact": None,
                    "application_key": "an-application-key",
                    "client_key": None,
                },
            }
        )
    )
    port = free_port()
    log_file = state_home.parent / "gateway.log"
    with log_file.open("wb") as log:
        gateway = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from hue_grpc.cli import main; raise SystemExit(main())",
                "--port",
                str(port),
            ],
            stdout=log,
            stderr=log,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    try:
        listening = await_listening_line(log_file, gateway)
        announced = listening["services"]
        assert SERVICE_NAME in announced  # type: ignore[operator]
        assert EVENT_SERVICE in announced  # type: ignore[operator]

        async def scenario() -> grpc.StatusCode:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                stub = lighting_grpc.LightingServiceStub(channel)
                try:
                    await stub.ListLights(lighting_service_pb2.ListLightsRequest())
                except grpc.aio.AioRpcError as refused:
                    return refused.code()
                raise AssertionError("a bridge on port 1 answered")

        assert run(scenario()) == grpc.StatusCode.UNAVAILABLE

        gateway.send_signal(signal.SIGTERM)
        assert gateway.wait(timeout=30) == 0
    finally:
        if gateway.poll() is None:  # pragma: no cover - only on a failure
            gateway.kill()
            gateway.wait(timeout=30)


# Static bridge configuration, which is how the NixOS module drives the server


@pytest.fixture
def captured_entry(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Serve without a network: record the entry `main` built and return."""
    seen: dict[str, object] = {}

    async def fake_serve(
        config: GatewayConfig,
        entry: object,
        *,
        ca_pem: str | None = None,
        event_queue_size: int,
    ) -> None:
        seen["entry"] = entry
        seen["ca_pem"] = ca_pem

    monkeypatch.setattr(cli, "_serve", fake_serve)
    return seen


def test_static_flags_build_an_entry_and_skip_the_registry(
    captured_entry: dict[str, object], state_home: Path, tmp_path: Path
) -> None:
    # An unreadable registry would abort the registry path; static config
    # never looks at it.
    state_home.parent.mkdir(parents=True)
    state_home.write_text("{ not a registry")
    credentials = tmp_path / "hue-credentials"
    credentials.write_text("application-key=static-key\nclient-key=static-client\n")

    code = main(
        [
            "--port",
            "0",
            "--bridge-address",
            "192.168.86.5",
            "--bridge-id",
            BRIDGE_ID,
            "--credentials-file",
            str(credentials),
        ]
    )

    assert code == 0
    entry = captured_entry["entry"]
    assert entry.bridge_id == BRIDGE_ID  # type: ignore[attr-defined]
    assert entry.address == "192.168.86.5"  # type: ignore[attr-defined]
    assert entry.application_key == "static-key"  # type: ignore[attr-defined]
    assert entry.client_key == "static-client"  # type: ignore[attr-defined]


def test_the_bridge_ca_file_reaches_the_transport(
    captured_entry: dict[str, object],
    bridge_certs: BridgeCerts,
    tmp_path: Path,
) -> None:
    credentials = tmp_path / "hue-credentials"
    credentials.write_text("application-key=static-key\n")

    code = main(
        [
            "--port",
            "0",
            "--bridge-address",
            "192.168.86.5",
            "--bridge-id",
            BRIDGE_ID,
            "--credentials-file",
            str(credentials),
            "--bridge-ca-file",
            str(bridge_certs.ca_file),
        ]
    )

    assert code == 0
    assert captured_entry["ca_pem"] == bridge_certs.ca_file.read_text()


def test_no_bridge_ca_file_leaves_the_vendored_root_in_place(
    captured_entry: dict[str, object], tmp_path: Path
) -> None:
    credentials = tmp_path / "hue-credentials"
    credentials.write_text("application-key=static-key\n")

    main(
        [
            "--port",
            "0",
            "--bridge-address",
            "192.168.86.5",
            "--bridge-id",
            BRIDGE_ID,
            "--credentials-file",
            str(credentials),
        ]
    )

    assert captured_entry["ca_pem"] is None


def test_a_missing_bridge_ca_file_is_a_line_not_a_traceback(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--bridge-ca-file", str(tmp_path / "absent.pem")])

    assert exit_info.value.code == 2
    printed = capsys.readouterr().err
    assert "could not read bridge CA file" in printed
    assert "Traceback" not in printed


def test_a_bridge_ca_file_that_is_not_a_certificate_is_refused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    not_a_cert = tmp_path / "junk.pem"
    not_a_cert.write_text("just some text\n")

    with pytest.raises(SystemExit) as exit_info:
        main(["--bridge-ca-file", str(not_a_cert)])

    assert exit_info.value.code == 2
    assert "no PEM certificate" in capsys.readouterr().err


def test_pairing_verifies_against_the_given_bridge_ca_file(
    monkeypatch: pytest.MonkeyPatch,
    state_home: Path,
    bridge_certs: BridgeCerts,
) -> None:
    seen: dict[str, object] = {}

    async def mint(
        *, address: str, bridge_id: str, instance: str, ca_pem: str | None = None
    ) -> PairedSecrets:
        seen["ca_pem"] = ca_pem
        return PairedSecrets(application_key="minted", client_key=None)

    monkeypatch.setattr(cli, "_mint", mint)

    code = main(
        [
            "pair",
            "--bridge-address",
            "192.168.86.223",
            "--bridge-id",
            BRIDGE_ID,
            "--bridge-ca-file",
            str(bridge_certs.ca_file),
        ]
    )

    assert code == 0
    assert seen["ca_pem"] == bridge_certs.ca_file.read_text()


def test_a_static_bridge_without_its_id_is_a_line_not_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--bridge-address", "192.168.86.5"])

    assert exit_info.value.code == 2
    printed = capsys.readouterr().err
    assert "--bridge-id" in printed
    assert "--credentials-file" in printed
    assert "Traceback" not in printed


def test_credentials_file_without_a_bridge_address_is_refused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--credentials-file", str(tmp_path / "creds")])

    assert exit_info.value.code == 2
    assert "only applies with --bridge-address" in capsys.readouterr().err


def test_an_unreadable_credentials_file_stops_the_gateway_with_a_line(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "--bridge-address",
                "192.168.86.5",
                "--bridge-id",
                BRIDGE_ID,
                "--credentials-file",
                str(tmp_path / "absent"),
            ]
        )

    assert exit_info.value.code == 2
    printed = capsys.readouterr().err
    assert "could not read" in printed
    assert "Traceback" not in printed


def test_the_binary_serves_a_statically_configured_bridge(
    state_home: Path, tmp_path: Path
) -> None:
    """As a process, the way the NixOS unit starts it: address and id as
    flags, the application key from a credentials file, no registry."""
    credentials = tmp_path / "hue-credentials"
    credentials.write_text("application-key=an-application-key\n")
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
                "--bridge-address",
                # Nothing listens on port 1, and nothing is meant to.
                "127.0.0.1:1",
                "--bridge-id",
                BRIDGE_ID,
                "--credentials-file",
                str(credentials),
            ],
            stdout=log,
            stderr=log,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    try:
        await_listening_line(log_file, gateway)

        async def scenario() -> grpc.StatusCode:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                stub = lighting_grpc.LightingServiceStub(channel)
                try:
                    await stub.ListLights(lighting_service_pb2.ListLightsRequest())
                except grpc.aio.AioRpcError as refused:
                    return refused.code()
                raise AssertionError("a bridge on port 1 answered")

        assert run(scenario()) == grpc.StatusCode.UNAVAILABLE
        assert not state_home.exists()

        gateway.send_signal(signal.SIGTERM)
        assert gateway.wait(timeout=30) == 0
    finally:
        if gateway.poll() is None:  # pragma: no cover - only on a failure
            gateway.kill()
            gateway.wait(timeout=30)
