"""Name conversions between OpenAPI and protobuf conventions."""

from __future__ import annotations

import re

_WORD_BOUNDARY = re.compile(r"[^0-9a-zA-Z]+")


def _words(name: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return [word for word in _WORD_BOUNDARY.split(spaced) if word]


def pascal_case(name: str) -> str:
    """`timed_effects` -> `TimedEffects`."""
    return "".join(word[:1].upper() + word[1:] for word in _words(name))


def screaming_snake_case(name: str) -> str:
    """`color_temperature` -> `COLOR_TEMPERATURE`."""
    return "_".join(word.upper() for word in _words(name))


def enum_value_name(enum_name: str, value: str) -> str:
    """Protobuf enum values share a C++ scope, so they carry the enum's prefix."""
    return f"{screaming_snake_case(enum_name)}_{screaming_snake_case(value)}"
