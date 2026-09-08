"""A running fake Bridge and an HTTPS client that trusts its CA."""

from __future__ import annotations

import ssl
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fake_hue.bridge import APPLICATION_KEY_HEADER, FakeHueBridge
from fake_hue.certs import mint_bridge_certs

BRIDGE_ID = "ECB5FAFFFE334703"
APPLICATION_KEY = "fake-application-key-do-not-use"


@pytest.fixture
def bridge(tmp_path: Path) -> Iterator[FakeHueBridge]:
    certs = mint_bridge_certs(tmp_path / "certs", bridge_id=BRIDGE_ID)
    with FakeHueBridge(certs, application_key=APPLICATION_KEY) as running:
        yield running


@pytest.fixture
def ca_context(bridge: FakeHueBridge, tmp_path: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(tmp_path / "certs" / "ca.pem"))
    # The leaf has no SAN and the test dials an address, exactly as the
    # Gateway does; identity is the CA plus the common-name check, not the
    # hostname.
    context.check_hostname = False
    return context


@pytest.fixture
def client(bridge: FakeHueBridge, ca_context: ssl.SSLContext) -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url=f"https://{bridge.address}",
        verify=ca_context,
        headers={APPLICATION_KEY_HEADER: APPLICATION_KEY},
    ) as opened:
        yield opened
