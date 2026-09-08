"""Tests for the protobuf intermediate model and its rendering."""

from __future__ import annotations

import pytest
from protogen.model import Enum, EnumValue, Field, Message, OneOf, ProtoFile


def render(**kwargs: object) -> str:
    return ProtoFile(package="hue.v1", **kwargs).render()  # type: ignore[arg-type]


def test_header_declares_proto3_and_the_package() -> None:
    out = render(messages=(Message(name="Light"),))

    assert 'syntax = "proto3";' in out
    assert "package hue.v1;" in out


def test_absent_field_is_optional() -> None:
    """Presence is the whole point: an omitted field must not become a default."""
    out = render(
        messages=(Message(name="M", fields=(Field("on", "bool", 1, optional=True),)),)
    )

    assert "optional bool on = 1;" in out


def test_required_field_is_not_optional() -> None:
    out = render(messages=(Message(name="M", fields=(Field("rid", "string", 1),)),))

    assert "string rid = 1;" in out
    assert "optional" not in out


def test_repeated_field_is_never_optional() -> None:
    """proto3 forbids `optional repeated`; the compiler rejects it."""
    out = render(
        messages=(
            Message(name="M", fields=(Field("errors", "Error", 1, repeated=True),)),
        )
    )

    assert "repeated Error errors = 1;" in out
    assert "optional" not in out


def test_optional_repeated_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="optional repeated"):
        Field("errors", "Error", 1, optional=True, repeated=True)


def test_duplicate_field_numbers_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate field number"):
        Message(name="M", fields=(Field("a", "string", 1), Field("b", "string", 1)))


def test_enum_renders_with_unspecified_zero() -> None:
    out = render(
        enums=(
            Enum(
                name="Mode",
                values=(
                    EnumValue("MODE_UNSPECIFIED", 0),
                    EnumValue("MODE_NORMAL", 1),
                ),
            ),
        )
    )

    assert "enum Mode {" in out
    assert "MODE_UNSPECIFIED = 0;" in out


def test_enum_without_a_zero_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="zero value"):
        Enum(name="Mode", values=(EnumValue("MODE_NORMAL", 1),))


def test_nested_messages_and_enums_are_indented() -> None:
    out = render(
        messages=(
            Message(
                name="Outer",
                messages=(Message(name="Inner", fields=(Field("a", "string", 1),)),),
            ),
        )
    )

    assert "  message Inner {" in out
    assert "    string a = 1;" in out


def test_oneof_members_carry_no_optional_keyword() -> None:
    """oneof members already have explicit presence."""
    out = render(
        messages=(
            Message(
                name="M",
                oneofs=(
                    OneOf(
                        name="colour",
                        fields=(Field("xy", "Color", 1), Field("mirek", "uint32", 2)),
                    ),
                ),
            ),
        )
    )

    assert "oneof colour {" in out
    assert "Color xy = 1;" in out
    assert "optional" not in out


def test_field_numbers_are_unique_across_oneofs_and_plain_fields() -> None:
    with pytest.raises(ValueError, match="duplicate field number"):
        Message(
            name="M",
            fields=(Field("a", "string", 1),),
            oneofs=(OneOf(name="g", fields=(Field("b", "string", 1),)),),
        )


def test_duplicate_enum_value_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        Enum(
            name="E",
            values=(
                EnumValue("E_UNSPECIFIED", 0),
                EnumValue("E_A", 1),
                EnumValue("E_A", 2),
            ),
        )


def test_duplicate_enum_numbers_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        Enum(
            name="E",
            values=(
                EnumValue("E_UNSPECIFIED", 0),
                EnumValue("E_A", 1),
                EnumValue("E_B", 1),
            ),
        )
