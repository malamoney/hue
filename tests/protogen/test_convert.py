"""Tests for turning OpenAPI schemas into the protobuf model."""

from __future__ import annotations

from typing import Any

import pytest
from protogen.convert import convert_document

SPEC: dict[str, Any] = {"components": {"schemas": {}}}


def build(schemas: dict[str, Any], roots: list[str] | None = None) -> str:
    spec = {"components": {"schemas": schemas}}
    return convert_document(spec, roots or list(schemas), package="hue.v1").render()


def test_field_absent_from_required_becomes_optional() -> None:
    out = build({"M": {"type": "object", "properties": {"on": {"type": "boolean"}}}})

    assert "optional bool on = 1;" in out


def test_field_listed_in_required_is_not_optional() -> None:
    out = build(
        {
            "M": {
                "type": "object",
                "required": ["rid"],
                "properties": {"rid": {"type": "string"}},
            }
        }
    )

    assert "string rid = 1;" in out
    assert "optional" not in out


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"type": "string"}, "string"),
        ({"type": "boolean"}, "bool"),
        ({"type": "integer"}, "int32"),
        ({"type": "integer", "format": "int64"}, "int64"),
        ({"type": "number"}, "double"),
        ({"type": "number", "format": "float"}, "float"),
    ],
)
def test_scalar_type_mapping(schema: dict[str, Any], expected: str) -> None:
    out = build({"M": {"type": "object", "properties": {"a": schema}}})

    assert f"optional {expected} a = 1;" in out


def test_array_becomes_repeated_and_not_optional() -> None:
    out = build(
        {
            "M": {
                "type": "object",
                "properties": {"xs": {"type": "array", "items": {"type": "string"}}},
            }
        }
    )

    assert "repeated string xs = 1;" in out
    assert "optional" not in out


def test_ref_becomes_a_message_reference_and_the_target_is_emitted() -> None:
    out = build(
        {
            "M": {
                "type": "object",
                "properties": {"on": {"$ref": "#/components/schemas/On"}},
            },
            "On": {"type": "object", "properties": {"on": {"type": "boolean"}}},
        },
        roots=["M"],
    )

    assert "optional .hue.v1.On on = 1;" in out
    assert "message On {" in out


def test_inline_enum_becomes_a_nested_enum_with_unspecified() -> None:
    out = build(
        {
            "M": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["normal", "streaming"]}
                },
            }
        }
    )

    assert "enum Mode {" in out
    assert "MODE_UNSPECIFIED = 0;" in out
    assert "MODE_NORMAL = 1;" in out
    assert "MODE_STREAMING = 2;" in out
    assert "optional Mode mode = 1;" in out


def test_inline_object_becomes_a_nested_message() -> None:
    out = build(
        {
            "M": {
                "type": "object",
                "properties": {
                    "timed_effects": {
                        "type": "object",
                        "properties": {"duration": {"type": "integer"}},
                    }
                },
            }
        }
    )

    assert "message TimedEffects {" in out
    assert "optional TimedEffects timed_effects = 1;" in out


def test_field_numbers_follow_declaration_order() -> None:
    out = build(
        {
            "M": {
                "type": "object",
                "properties": {
                    "a": {"type": "string"},
                    "b": {"type": "string"},
                    "c": {"type": "string"},
                },
            }
        }
    )

    assert "optional string a = 1;" in out
    assert "optional string b = 2;" in out
    assert "optional string c = 3;" in out


def test_allof_composition_is_flattened_into_one_message() -> None:
    out = build(
        {
            "Base": {"type": "object", "properties": {"id": {"type": "string"}}},
            "M": {
                "allOf": [
                    {"$ref": "#/components/schemas/Base"},
                    {"type": "object", "properties": {"extra": {"type": "string"}}},
                ]
            },
        },
        roots=["M"],
    )

    assert "optional string id = 1;" in out
    assert "optional string extra = 2;" in out


def test_a_transitively_referenced_schema_is_emitted_once() -> None:
    out = build(
        {
            "M": {
                "type": "object",
                "properties": {
                    "a": {"$ref": "#/components/schemas/Shared"},
                    "b": {"$ref": "#/components/schemas/Shared"},
                },
            },
            "Shared": {"type": "object", "properties": {"x": {"type": "string"}}},
        },
        roots=["M"],
    )

    assert out.count("message Shared {") == 1


def test_ref_to_a_scalar_schema_inlines_the_scalar() -> None:
    """A $ref target with no properties is not a message.

    Emitting an empty message here would silently drop the value: Hue's
    Brightness is a bare number, and a fieldless message cannot carry it.
    """
    out = build(
        {
            "M": {
                "type": "object",
                "properties": {
                    "brightness": {"$ref": "#/components/schemas/Brightness"}
                },
            },
            "Brightness": {"type": "number", "minimum": 0, "maximum": 100},
        },
        roots=["M"],
    )

    assert "optional double brightness = 1;" in out
    assert "message Brightness" not in out


def test_ref_to_an_enum_schema_becomes_a_top_level_enum() -> None:
    out = build(
        {
            "M": {
                "type": "object",
                "properties": {"archetype": {"$ref": "#/components/schemas/Archetype"}},
            },
            "Archetype": {"type": "string", "enum": ["bollard", "ceiling_round"]},
        },
        roots=["M"],
    )

    assert "enum Archetype {" in out
    assert "ARCHETYPE_UNSPECIFIED = 0;" in out
    assert "ARCHETYPE_BOLLARD = 1;" in out
    assert "optional .hue.v1.Archetype archetype = 1;" in out
    assert "message Archetype" not in out


def test_component_ref_is_qualified_so_a_nested_message_cannot_capture_it() -> None:
    """Protobuf resolves names innermost-first, C++ style.

    An inline object named `On` nested inside the message would otherwise
    capture a reference meant for the top-level `On`, making the field
    self-recursive and the real value unreachable.
    """
    out = build(
        {
            "M": {
                "type": "object",
                "properties": {
                    "on": {
                        "type": "object",
                        "properties": {"on": {"$ref": "#/components/schemas/On"}},
                    }
                },
            },
            "On": {"type": "object", "properties": {"on": {"type": "boolean"}}},
        },
        roots=["M"],
    )

    assert "optional .hue.v1.On on = 1;" in out


def test_property_wrapped_in_allof_is_resolved() -> None:
    """`{allOf: [{$ref: ...}], description: ...}` is a common OpenAPI idiom."""
    out = build(
        {
            "M": {
                "type": "object",
                "properties": {
                    "a": {
                        "allOf": [{"$ref": "#/components/schemas/Thing"}],
                        "description": "wrapped",
                    }
                },
            },
            "Thing": {"type": "string"},
        },
        roots=["M"],
    )

    assert "optional string a = 1;" in out


def test_array_items_wrapped_in_allof_is_resolved() -> None:
    out = build(
        {
            "M": {
                "type": "object",
                "properties": {
                    "xs": {
                        "type": "array",
                        "items": {"allOf": [{"$ref": "#/components/schemas/Thing"}]},
                    }
                },
            },
            "Thing": {"type": "string"},
        },
        roots=["M"],
    )

    assert "repeated string xs = 1;" in out


def test_enum_values_that_normalise_identically_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        build(
            {
                "M": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "string", "enum": ["on_off", "on-off"]}
                    },
                }
            }
        )


def test_non_string_enum_values_are_rejected_clearly() -> None:
    with pytest.raises(ValueError, match="non-string"):
        build(
            {
                "M": {
                    "type": "object",
                    "properties": {"a": {"type": "integer", "enum": [1, 2]}},
                }
            }
        )
