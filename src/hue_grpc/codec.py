"""Hue's JSON on one side, protobuf on the other, with presence intact.

Two directions, and they are not mirror images. Reading a Resource is
forgiving: a Bridge running newer firmware sends fields and enum values this
Gateway has never heard of, and a light must stay readable when it does.
Writing a command is strict: everything sent has been asked for by a client,
so a value Hue would reject is refused here, where the error can say which
field it was.

**Presence is the contract.** A `LightPut` field that was never set is absent
from the JSON the Bridge receives, and a field that was set is present even
when its value is zero — `on { on: false }` turns a light off, and an unset
`on` leaves it alone. Protobuf draws that line with `optional` and with
message fields; this module does nothing but carry it across. Inside a
submessage the caller did set, a scalar with no presence of its own is part of
what they set: `dimming { brightness: 0 }` means zero, not silence.

Nothing here knows what a Light is. The generated descriptors say what the
shape is, and `hue_grpc.lighting` says which ranges Hue accepts, so the event
stream can decode its Resources through the same code.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from functools import cache
from types import MappingProxyType
from typing import Any

from google.protobuf.descriptor import EnumDescriptor, FieldDescriptor
from google.protobuf.message import Message

__all__ = [
    "CodecError",
    "InvalidCommandError",
    "MalformedResourceError",
    "decode",
    "encode",
]

_log = logging.getLogger(__name__)

#: Ranges are keyed by a field's fully qualified name — `hue.v1.Dimming.
#: brightness` — so a bound follows the type wherever it is reused.
Ranges = Mapping[str, tuple[float, float]]

_NO_RANGES: Ranges = MappingProxyType({})

_WORD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_INTEGERS = frozenset(
    {
        FieldDescriptor.CPPTYPE_INT32,
        FieldDescriptor.CPPTYPE_INT64,
        FieldDescriptor.CPPTYPE_UINT32,
        FieldDescriptor.CPPTYPE_UINT64,
    }
)
_REALS = frozenset({FieldDescriptor.CPPTYPE_DOUBLE, FieldDescriptor.CPPTYPE_FLOAT})


class CodecError(Exception):
    """A Resource or a command could not be carried across."""


class MalformedResourceError(CodecError):
    """The Bridge sent something the schema cannot hold.

    Names the field, because "the bridge sent bad JSON" about a Resource with
    two hundred of them is not a thing anyone can act on.
    """


class InvalidCommandError(CodecError):
    """A command cannot become a Hue request, and must not be sent as one.

    A partly-applied mutation cannot be undone by retrying, so a command Hue
    would reject is stopped here rather than discovered halfway through.
    """


def decode[MessageT: Message](payload: Mapping[str, Any], into: MessageT) -> MessageT:
    """Fill `into` from one Hue Resource, and hand it back.

    Unknown fields and unknown enum values are survivable and survived: the
    Bridge's firmware moves on its own schedule, and a light that gains a
    property this Gateway has never seen still has a brightness. Anything
    whose *shape* is wrong is not survivable and raises.
    """
    _fill(into, payload, into.DESCRIPTOR.name)
    return into


def encode(message: Message, *, ranges: Ranges = _NO_RANGES) -> dict[str, Any]:
    """The Hue JSON body for `message`, carrying only what was set."""
    return _document(message, ranges, message.DESCRIPTOR.name)


def _fill(message: Message, payload: Any, path: str) -> None:
    if not isinstance(payload, Mapping):
        raise MalformedResourceError(f"{path} is {_kind(payload)}, expected an object")
    for name, value in payload.items():
        field = message.DESCRIPTOR.fields_by_name.get(name)
        if field is None:
            # Newer firmware, or a Resource property outside our subset.
            _log.debug("ignoring unknown field %s.%s", path, name)
            continue
        if value is None:
            # Hue writes an absent property as an absent key, so a null is an
            # oddity rather than a value; either way there is nothing to hold.
            continue
        _read(message, field, value, f"{path}.{name}")


def _read(message: Message, field: FieldDescriptor, value: Any, path: str) -> None:
    if field.is_repeated:
        if not isinstance(value, list):
            raise MalformedResourceError(f"{path} is {_kind(value)}, expected a list")
        repeated = getattr(message, field.name)
        for index, item in enumerate(value):
            if field.type == FieldDescriptor.TYPE_MESSAGE:
                _fill(repeated.add(), item, f"{path}[{index}]")
            else:
                repeated.append(_scalar(field, item, f"{path}[{index}]"))
        return
    if field.type == FieldDescriptor.TYPE_MESSAGE:
        submessage = getattr(message, field.name)
        # An empty object is still the Bridge saying the field is there, and
        # filling nothing in would leave it looking absent.
        submessage.SetInParent()
        _fill(submessage, value, path)
        return
    setattr(message, field.name, _scalar(field, value, path))


def _scalar(field: FieldDescriptor, value: Any, path: str) -> Any:
    if field.type == FieldDescriptor.TYPE_ENUM:
        return _enum_number(field.enum_type, value, path)
    # `bool` is an `int` in Python and would otherwise pass for a number.
    if field.cpp_type in _INTEGERS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise MalformedResourceError(
                f"{path} is {_kind(value)}, expected a whole number"
            )
        return value
    if field.cpp_type in _REALS:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise MalformedResourceError(f"{path} is {_kind(value)}, expected a number")
        return float(value)
    if field.cpp_type == FieldDescriptor.CPPTYPE_BOOL:
        if not isinstance(value, bool):
            raise MalformedResourceError(
                f"{path} is {_kind(value)}, expected true or false"
            )
        return value
    if not isinstance(value, str):
        raise MalformedResourceError(f"{path} is {_kind(value)}, expected text")
    return value


def _enum_number(enum: EnumDescriptor, value: Any, path: str) -> int:
    if not isinstance(value, str):
        raise MalformedResourceError(
            f"{path} is {_kind(value)}, expected one of {enum.name}'s names"
        )
    number = _numbers_by_hue_name(enum).get(value.lower())
    if number is None:
        # A value from firmware newer than this Gateway. Refusing the whole
        # Resource over one property nobody has asked for yet would make a
        # Hue update take the lights out.
        _log.debug("unknown %s value %r at %s", enum.full_name, value, path)
        return 0
    return number


def _document(message: Message, ranges: Ranges, path: str) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for field in message.DESCRIPTOR.fields:
        field_path = f"{path}.{field.name}"
        if field.is_repeated:
            values = list(getattr(message, field.name))
            if not values:
                continue
            document[field.name] = [
                _written(field, item, ranges, f"{field_path}[{index}]")
                for index, item in enumerate(values)
            ]
            continue
        if field.has_presence:
            if not message.HasField(field.name):
                continue
        elif field.type == FieldDescriptor.TYPE_ENUM and not getattr(
            message, field.name
        ):
            # No presence and the zero value: indistinguishable from nobody
            # having chosen, so it is not sent as a choice.
            continue
        document[field.name] = _written(
            field, getattr(message, field.name), ranges, field_path
        )
    return document


def _written(field: FieldDescriptor, value: Any, ranges: Ranges, path: str) -> Any:
    if field.type == FieldDescriptor.TYPE_MESSAGE:
        return _document(value, ranges, path)
    if field.type == FieldDescriptor.TYPE_ENUM:
        return _enum_name(field, value, path)
    if field.cpp_type in _INTEGERS or field.cpp_type in _REALS:
        _within_range(field, value, ranges, path)
    return value


def _enum_name(field: FieldDescriptor, number: int, path: str) -> str:
    value = field.enum_type.values_by_number.get(number)
    if value is None:
        raise InvalidCommandError(
            f"{path} is {number}, which is not a value of {field.enum_type.name}"
        )
    if number == 0:
        raise InvalidCommandError(
            f"{path} is {value.name}, which asks the bridge for nothing in "
            f"particular; leave the field unset to leave it alone"
        )
    return _hue_names_by_number(field.enum_type)[number]


def _within_range(
    field: FieldDescriptor, value: float, ranges: Ranges, path: str
) -> None:
    bounds = ranges.get(field.full_name)
    if bounds is None:
        return
    low, high = bounds
    if not low <= value <= high:
        raise InvalidCommandError(
            f"{path} is {value:g}, outside the {low:g} to {high:g} that "
            f"{field.full_name} allows"
        )


@cache
def _numbers_by_hue_name(enum: EnumDescriptor) -> Mapping[str, int]:
    prefix = _screaming_snake_case(enum.name) + "_"
    return MappingProxyType(
        {_hue_name(value.name, prefix): value.number for value in enum.values}
    )


@cache
def _hue_names_by_number(enum: EnumDescriptor) -> Mapping[int, str]:
    prefix = _screaming_snake_case(enum.name) + "_"
    return MappingProxyType(
        {value.number: _hue_name(value.name, prefix) for value in enum.values}
    )


def _hue_name(name: str, prefix: str) -> str:
    """`SUPPORTED_EFFECTS_NO_EFFECT` -> `no_effect`.

    Protobuf enum values share a C++ scope and so carry their enum's name as a
    prefix; Hue's own spelling is what is left once it is taken back off.
    """
    return (name[len(prefix) :] if name.startswith(prefix) else name).lower()


def _screaming_snake_case(name: str) -> str:
    """`SupportedEffects` -> `SUPPORTED_EFFECTS`, as the generator names them."""
    return _WORD_BOUNDARY.sub("_", name).upper()


def _kind(value: Any) -> str:
    """What arrived, in JSON's words rather than Python's."""
    return {
        type(None): "null",
        bool: "a boolean",
        int: "a whole number",
        float: "a number",
        str: "text",
        list: "a list",
        dict: "an object",
    }.get(type(value), f"a {type(value).__name__}")
