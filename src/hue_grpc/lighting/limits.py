"""What Hue will accept in a command, checked before anything is sent.

Every bound here is copied from `openapi.yaml`, where it sits on the schema
the field was generated from. They are written down rather than derived
because the generator emits protobuf, which has no way to carry a range, and
because a mutation that Hue rejects halfway is not something a client can undo
by asking again — the request never leaves if a number is out of bounds.

Keys are fully qualified protobuf field names, so a bound follows its type: a
`GamutPosition` is as bounded inside a gradient point as it is in `color.xy`.
"""

from __future__ import annotations

from types import MappingProxyType

from hue_grpc.codec import Ranges

__all__ = ["COMMAND_RANGES"]

COMMAND_RANGES: Ranges = MappingProxyType(
    {
        "hue.v1.Dimming.brightness": (0.0, 100.0),
        "hue.v1.DimmingDelta.brightness_delta": (0.0, 100.0),
        "hue.v1.ColorTemperature.mirek": (153.0, 500.0),
        "hue.v1.ColorTemperatureDelta.mirek_delta": (0.0, 347.0),
        "hue.v1.GamutPosition.x": (0.0, 1.0),
        "hue.v1.GamutPosition.y": (0.0, 1.0),
        "hue.v1.Signaling.duration": (0.0, 65534000.0),
        "hue.v1.EffectsV2Parameters.speed": (0.0, 1.0),
        "hue.v1.EffectsV2Parameters.ColorTemperature.mirek": (153.0, 500.0),
        "hue.v1.Powerup.Dimming.dimming": (0.0, 100.0),
        # The spec says maximum 0, which would make the field unusable and is
        # a defect in it: Hue documents dynamics speed as a 0-to-1 fraction,
        # the same as every other speed here. Verify against real hardware in
        # issue #16 before trusting either number.
        "hue.v1.LightDynamics.speed": (0.0, 1.0),
    }
)
