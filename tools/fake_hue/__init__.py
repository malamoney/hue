"""A fake Philips Hue Bridge, for tests that need one without hardware.

``fake_hue.certs`` mints the Bridge-shaped certificate; ``fake_hue.bridge`` is
the HTTPS server that speaks the CLIP v2 subset the Gateway uses. ``python -m
fake_hue`` runs it as a standalone process, which is how the NixOS VM
integration test (issue #14) stands one up on its own node.
"""

from __future__ import annotations

from fake_hue.bridge import FakeHueBridge, LightStore
from fake_hue.certs import BridgeCerts, mint_bridge_certs

__all__ = [
    "BridgeCerts",
    "FakeHueBridge",
    "LightStore",
    "mint_bridge_certs",
]
