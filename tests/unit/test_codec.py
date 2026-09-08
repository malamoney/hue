"""Hue's JSON in and out of protobuf, with presence intact.

The mapping is where this service is most likely to be quietly wrong. A
brightness that arrives as `0` because nobody set it, an unknown enum value
that raises instead of degrading, a light whose capabilities are dropped on
the way through — none of those fail loudly, and all of them are here.
"""

from __future__ import annotations

import pytest

from hue.v1 import common_pb2, lighting_pb2
from hue_grpc.codec import InvalidCommandError, MalformedResourceError, decode, encode
from hue_grpc.lighting.limits import COMMAND_RANGES


def put(**payload: object) -> dict[str, object]:
    return dict(payload)


def command(message: lighting_pb2.LightPut) -> dict[str, object]:
    return encode(message, ranges=COMMAND_RANGES)


# Reading a light


def test_decodes_a_complete_light() -> None:
    light = decode(
        {
            "type": "light",
            "id": "8f2a1e00-0000-4000-8000-000000000001",
            "id_v1": "/lights/3",
            "owner": {"rid": "1e00", "rtype": "device"},
            "metadata": {"name": "Desk", "archetype": "sultan_bulb"},
            "on": {"on": True},
            "dimming": {"brightness": 42.5, "min_dim_level": 0.2},
            "color_temperature": {
                "mirek": 366,
                "mirek_valid": True,
                "mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 500},
            },
            "color": {
                "xy": {"x": 0.4573, "y": 0.41},
                "gamut_type": "C",
                "gamut": {"red": {"x": 0.6915, "y": 0.3083}},
            },
            "mode": "normal",
            "effects": {"status": "no_effect", "status_values": ["no_effect", "fire"]},
        },
        lighting_pb2.LightGet(),
    )

    assert light.id == "8f2a1e00-0000-4000-8000-000000000001"
    assert light.owner.rtype == common_pb2.ResourceIdentifier.RTYPE_DEVICE
    assert light.metadata.archetype == common_pb2.LIGHT_ARCHETYPE_SULTAN_BULB
    assert light.on.on is True
    assert light.dimming.brightness == 42.5
    assert light.color_temperature.mirek_schema.mirek_maximum == 500
    assert light.color.gamut_type == lighting_pb2.LightGet.Color.GAMUT_TYPE_C
    assert light.color.gamut.red.x == 0.6915
    assert light.mode == lighting_pb2.LightGet.MODE_NORMAL
    assert list(light.effects.status_values) == [
        common_pb2.SUPPORTED_EFFECTS_NO_EFFECT,
        common_pb2.SUPPORTED_EFFECTS_FIRE,
    ]


def test_a_minimal_light_leaves_everything_else_unset() -> None:
    """A dimmable-only light has no colour, and must not appear to have black."""
    light = decode(
        {"type": "light", "id": "abc", "on": {"on": False}}, lighting_pb2.LightGet()
    )

    assert light.HasField("on")
    assert light.on.on is False
    assert not light.HasField("color")
    assert not light.HasField("dimming")
    assert not light.HasField("color_temperature")


def test_capabilities_survive_the_crossing() -> None:
    """What a light supports is why a client asks; dropping it is a silent bug."""
    light = decode(
        {
            "id": "abc",
            "dimming": {"min_dim_level": 0.20000000298},
            "color": {"gamut": {"red": {"x": 0.6915, "y": 0.3083}}},
            "gradient": {"points_capable": 5, "pixel_count": 24},
            "signaling": {"signal_values": ["no_signal", "on_off"]},
            "timed_effects": {"effect_values": ["sunrise", "no_effect"]},
        },
        lighting_pb2.LightGet(),
    )

    assert light.dimming.min_dim_level == pytest.approx(0.2)
    assert light.gradient.points_capable == 5
    assert light.gradient.pixel_count == 24
    assert list(light.signaling.signal_values) == [
        common_pb2.SUPPORTED_SIGNALS_NO_SIGNAL,
        common_pb2.SUPPORTED_SIGNALS_ON_OFF,
    ]
    assert len(light.timed_effects.effect_values) == 2


def test_an_unknown_enum_value_reads_as_unspecified() -> None:
    """New firmware naming a new archetype must not make a light unreadable."""
    light = decode(
        {"id": "abc", "metadata": {"name": "Hall", "archetype": "hue_starfield"}},
        lighting_pb2.LightGet(),
    )

    assert light.metadata.name == "Hall"
    assert light.metadata.archetype == common_pb2.LIGHT_ARCHETYPE_UNSPECIFIED


def test_an_unknown_field_is_ignored() -> None:
    light = decode({"id": "abc", "quantum_flux": {"level": 3}}, lighting_pb2.LightGet())

    assert light.id == "abc"


def test_a_null_reads_as_absent() -> None:
    light = decode({"id": "abc", "dimming": None}, lighting_pb2.LightGet())

    assert not light.HasField("dimming")


@pytest.mark.parametrize(
    ("payload", "path"),
    [
        ({"id": 7}, "LightGet.id"),
        ({"dimming": 5}, "LightGet.dimming"),
        ({"on": {"on": "yes"}}, "LightGet.on.on"),
        ({"dimming": {"brightness": "half"}}, "LightGet.dimming.brightness"),
        ({"color_temperature": {"mirek": 1.5}}, "LightGet.color_temperature.mirek"),
        ({"metadata": {"archetype": 4}}, "LightGet.metadata.archetype"),
        ({"effects": {"status_values": "fire"}}, "LightGet.effects.status_values"),
        ({"gradient": {"points": [3]}}, "LightGet.gradient.points[0]"),
    ],
)
def test_malformed_upstream_data_says_which_field(
    payload: dict[str, object], path: str
) -> None:
    with pytest.raises(MalformedResourceError) as raised:
        decode({"id": "abc", **payload}, lighting_pb2.LightGet())

    assert path in str(raised.value)


def test_a_payload_that_is_not_an_object_is_malformed() -> None:
    with pytest.raises(MalformedResourceError):
        decode(["a light"], lighting_pb2.LightGet())  # type: ignore[arg-type]


def test_an_integer_is_accepted_where_a_double_belongs() -> None:
    """JSON writes 100 for 100.0, and the Bridge does exactly that."""
    light = decode({"dimming": {"brightness": 100}}, lighting_pb2.LightGet())

    assert light.dimming.brightness == 100.0


def test_a_boolean_is_not_a_number() -> None:
    with pytest.raises(MalformedResourceError):
        decode({"dimming": {"brightness": True}}, lighting_pb2.LightGet())


# Writing a command


def test_an_empty_command_sends_nothing() -> None:
    assert command(lighting_pb2.LightPut()) == {}


def test_turning_a_light_off_is_not_an_omission() -> None:
    """The bug this whole module exists to prevent."""
    message = lighting_pb2.LightPut()
    message.on.on = False

    assert command(message) == {"on": {"on": False}}


def test_dimming_to_zero_is_not_an_omission() -> None:
    message = lighting_pb2.LightPut()
    message.dimming.brightness = 0

    assert command(message) == {"dimming": {"brightness": 0.0}}


def test_setting_brightness_leaves_power_alone() -> None:
    message = lighting_pb2.LightPut()
    message.dimming.brightness = 60

    assert command(message) == {"dimming": {"brightness": 60.0}}
    assert "on" not in command(message)


def test_enums_go_out_in_hue_s_spelling() -> None:
    message = lighting_pb2.LightPut()
    message.mode = lighting_pb2.LightPut.MODE_STREAMING
    message.dimming_delta.action = common_pb2.DimmingDelta.ACTION_UP
    message.dimming_delta.brightness_delta = 10

    assert command(message) == {
        "mode": "streaming",
        "dimming_delta": {"action": "up", "brightness_delta": 10.0},
    }


def test_nested_and_repeated_fields_are_carried() -> None:
    message = lighting_pb2.LightPut()
    message.gradient.points.add().xy.x = 0.3
    message.gradient.points.add().xy.y = 0.6

    assert command(message) == {
        "gradient": {
            "points": [{"xy": {"x": 0.3, "y": 0.0}}, {"xy": {"x": 0.0, "y": 0.6}}]
        }
    }


def test_the_colour_oneof_sends_only_what_was_set() -> None:
    message = lighting_pb2.LightPut()
    message.color_temperature.mirek = 366

    assert command(message) == {"color_temperature": {"mirek": 366}}

    message.color.xy.x = 0.4
    message.color.xy.y = 0.4

    assert command(message) == {"color": {"xy": {"x": 0.4, "y": 0.4}}}


@pytest.mark.parametrize("brightness", [0.0, 100.0, 0.001, 99.999])
def test_brightness_at_the_boundaries_is_allowed(brightness: float) -> None:
    message = lighting_pb2.LightPut()
    message.dimming.brightness = brightness

    assert command(message)["dimming"] == {"brightness": brightness}


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (lambda m: setattr(m.dimming, "brightness", 100.5), "Dimming.brightness"),
        (lambda m: setattr(m.dimming, "brightness", -1), "Dimming.brightness"),
        (
            lambda m: setattr(m.color_temperature, "mirek", 152),
            "ColorTemperature.mirek",
        ),
        (
            lambda m: setattr(m.color_temperature, "mirek", 501),
            "ColorTemperature.mirek",
        ),
        (lambda m: setattr(m.color.xy, "x", 1.5), "GamutPosition.x"),
        (lambda m: setattr(m.color.xy, "y", -0.5), "GamutPosition.y"),
        (
            lambda m: setattr(m.color_temperature_delta, "mirek_delta", 348),
            "ColorTemperatureDelta.mirek_delta",
        ),
        (lambda m: setattr(m.signaling, "duration", -1), "Signaling.duration"),
    ],
)
def test_a_value_outside_hue_s_range_never_reaches_the_bridge(
    mutate: object, path: str
) -> None:
    message = lighting_pb2.LightPut()
    mutate(message)  # type: ignore[operator]

    with pytest.raises(InvalidCommandError) as raised:
        command(message)

    assert path in str(raised.value)


def test_an_unspecified_enum_is_refused_rather_than_guessed() -> None:
    message = lighting_pb2.LightPut()
    message.mode = lighting_pb2.LightPut.MODE_UNSPECIFIED

    with pytest.raises(InvalidCommandError):
        command(message)


def test_ranges_are_checked_wherever_the_type_appears() -> None:
    """The gradient's points are `GamutPosition`s too, and just as bounded."""
    message = lighting_pb2.LightPut()
    message.gradient.points.add().xy.x = 4

    with pytest.raises(InvalidCommandError):
        command(message)
