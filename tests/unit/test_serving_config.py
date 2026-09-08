"""The listener's configuration, and the safety rules it refuses to break.

The defaults are the whole point: loopback, TLS off, no Gateway Token. Moving
the Gateway onto the LAN has to be a configuration change and nothing else, so
these tests pin both the defaults and what the config insists on before it
will listen anywhere but loopback.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hue_grpc.serving.config import (
    DEFAULT_ADDRESS,
    DEFAULT_PORT,
    GatewayConfig,
    TlsConfig,
    read_gateway_token,
)


def tls(tmp_path: Path) -> TlsConfig:
    certificate = tmp_path / "gateway.pem"
    private_key = tmp_path / "gateway.key"
    certificate.write_text("-----BEGIN CERTIFICATE-----\n")
    private_key.write_text("-----BEGIN PRIVATE KEY-----\n")
    return TlsConfig(certificate_file=certificate, private_key_file=private_key)


def test_defaults_listen_on_loopback_without_tls_or_a_token() -> None:
    config = GatewayConfig()

    assert config.address == DEFAULT_ADDRESS == "127.0.0.1"
    assert config.port == DEFAULT_PORT == 50051
    assert config.tls is None
    assert config.gateway_token is None
    assert config.is_loopback


def test_listen_target_brackets_an_ipv6_address() -> None:
    assert GatewayConfig().listen_target == "127.0.0.1:50051"
    assert GatewayConfig(address="::1").listen_target == "[::1]:50051"


def test_reflection_follows_the_listener_unless_it_is_asked_for(
    tmp_path: Path,
) -> None:
    """Loopback is a developer's machine; a LAN listener is not."""
    assert GatewayConfig().reflection_enabled
    assert not GatewayConfig(
        address="0.0.0.0", tls=tls(tmp_path), gateway_token="a-token"
    ).reflection_enabled

    assert not GatewayConfig(reflection=False).reflection_enabled
    assert GatewayConfig(
        address="0.0.0.0", tls=tls(tmp_path), gateway_token="a-token", reflection=True
    ).reflection_enabled


def test_a_listener_beyond_loopback_needs_tls_and_a_token(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tls"):
        GatewayConfig(address="0.0.0.0", gateway_token="a-token")

    with pytest.raises(ValueError, match="token"):
        GatewayConfig(address="192.168.86.10", tls=tls(tmp_path))

    # Both together is the config flip the Gateway is built to allow.
    GatewayConfig(address="192.168.86.10", tls=tls(tmp_path), gateway_token="a-token")


def test_the_address_must_be_an_ip_literal() -> None:
    """A hostname resolves; whether it resolves to loopback cannot be read off
    the configuration, and every rule here turns on that answer."""
    with pytest.raises(ValueError, match="localhost"):
        GatewayConfig(address="localhost")


def test_port_zero_asks_the_operating_system_for_one() -> None:
    """How the tests bind without picking a number and hoping."""
    assert GatewayConfig(port=0).port == 0


def test_refuses_nonsense_limits() -> None:
    for kwargs in (
        {"port": -1},
        {"port": 70000},
        {"max_inbound_message_bytes": 0},
        {"max_outbound_message_bytes": -1},
        {"default_deadline": 0.0},
        {"shutdown_grace": -1.0},
    ):
        with pytest.raises(ValueError):
            GatewayConfig(**kwargs)


def test_tls_needs_both_halves(tmp_path: Path) -> None:
    """Half a keypair fails at bind time otherwise, long after the config was
    written and read."""
    configured = tls(tmp_path)

    with pytest.raises(ValueError, match="private key"):
        TlsConfig(
            certificate_file=configured.certificate_file,
            private_key_file=tmp_path / "missing.key",
        )
    with pytest.raises(ValueError, match="certificate"):
        TlsConfig(
            certificate_file=tmp_path / "missing.pem",
            private_key_file=configured.private_key_file,
        )


def test_a_token_is_read_from_a_file_not_from_a_flag(tmp_path: Path) -> None:
    """`ps` shows every argument the service was started with."""
    token_file = tmp_path / "gateway-token"
    token_file.write_text("a-secret-token\n")
    token_file.chmod(0o400)

    assert read_gateway_token(token_file) == "a-secret-token"


def test_an_empty_token_file_is_a_misconfiguration_not_an_empty_token(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "gateway-token"
    token_file.write_text("\n")

    with pytest.raises(ValueError, match="empty"):
        read_gateway_token(token_file)


def test_a_token_file_others_can_read_is_worth_saying_out_loud(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    token_file = tmp_path / "gateway-token"
    token_file.write_text("a-secret-token\n")
    token_file.chmod(0o644)

    with caplog.at_level(logging.WARNING):
        assert read_gateway_token(token_file) == "a-secret-token"

    assert "0644" in caplog.text
    assert "a-secret-token" not in caplog.text


def test_the_config_never_carries_the_token_into_a_traceback(
    tmp_path: Path,
) -> None:
    config = GatewayConfig(
        address="192.168.86.10", tls=tls(tmp_path), gateway_token="a-secret-token"
    )

    assert "a-secret-token" not in repr(config)
    assert "<redacted>" in repr(config)
