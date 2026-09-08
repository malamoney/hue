"""Reading what the Bridge said when it refused.

The Bridge speaks two error dialects and this is the only reader of both, so
the tests carry real envelopes rather than shapes invented to match the code.
"""

from __future__ import annotations

from typing import Any

from hue_grpc.hue import errors

#: What CLIP v2 answers a bad `PUT` with, verbatim.
CLIP_V2 = {
    "errors": [
        {"description": "invalid value, dimming.brightness, 101"},
        {"description": "device (light) has communication issues"},
    ]
}

#: What `POST /api` answers before anybody presses the button, verbatim.
V1 = [
    {
        "error": {
            "type": 101,
            "address": "",
            "description": "link button not pressed",
        }
    }
]


def test_reads_every_description_out_of_a_clip_v2_envelope() -> None:
    assert errors.descriptions(CLIP_V2) == [
        "invalid value, dimming.brightness, 101",
        "device (light) has communication issues",
    ]


def test_reads_the_v1_dialect_too() -> None:
    """Pairing is on the old API, and its refusals arrive shaped differently."""
    assert errors.descriptions(V1) == ["link button not pressed"]
    assert errors.descriptions(V1[0]) == ["link button not pressed"]


def test_a_bridge_that_said_nothing_usable_is_not_a_second_failure() -> None:
    """This is read on a path that is already failing; it must not add one."""
    unusable: tuple[Any, ...] = (
        None,
        "<html><body>503 Service Unavailable</body></html>",
        [],
        {},
        {"errors": []},
        {"errors": "not a list"},
        {"errors": ["not an object"]},
        {"errors": [{"type": 7}]},
        {"error": None},
        {"errors": [{"description": "   "}]},
        42,
    )

    for payload in unusable:
        assert errors.descriptions(payload) == []


def test_keeps_a_description_whole_rather_than_summarising_it() -> None:
    """Hue names the field it rejected; that is the useful half."""
    said = errors.descriptions({"errors": [{"description": "  spaced out  "}]})

    assert said == ["spaced out"]
