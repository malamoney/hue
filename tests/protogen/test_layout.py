"""Tests for splitting generated schemas across multiple proto files."""

from __future__ import annotations

from typing import Any

import pytest
from protogen.layout import FileSpec, generate_files

SCHEMAS: dict[str, Any] = {
    "LightGet": {
        "type": "object",
        "properties": {"on": {"$ref": "#/components/schemas/On"}},
    },
    "Event": {
        "type": "object",
        "properties": {"owner": {"$ref": "#/components/schemas/ResourceIdentifier"}},
    },
    "On": {"type": "object", "properties": {"on": {"type": "boolean"}}},
    "ResourceIdentifier": {
        "type": "object",
        "required": ["rid"],
        "properties": {"rid": {"type": "string"}},
    },
}


def build(
    files: list[FileSpec], default: str = "hue/v1/common.proto"
) -> dict[str, str]:
    rendered = generate_files(
        {"components": {"schemas": SCHEMAS}},
        files,
        package="hue.v1",
        default_file=default,
    )
    return {path: proto.render() for path, proto in rendered.items()}


def test_each_root_lands_in_its_own_file() -> None:
    out = build(
        [
            FileSpec("hue/v1/lighting.proto", roots=["LightGet"]),
            FileSpec("hue/v1/events.proto", roots=["Event"]),
        ]
    )

    assert "message LightGet {" in out["hue/v1/lighting.proto"]
    assert "message Event {" in out["hue/v1/events.proto"]
    assert "message LightGet {" not in out["hue/v1/events.proto"]


def test_transitively_reached_schemas_go_to_the_default_file() -> None:
    """Otherwise every shared dependency would need assigning by hand."""
    out = build([FileSpec("hue/v1/lighting.proto", roots=["LightGet"])])

    assert "message On {" in out["hue/v1/common.proto"]
    assert "message On {" not in out["hue/v1/lighting.proto"]


def test_a_file_imports_the_files_it_references() -> None:
    out = build([FileSpec("hue/v1/lighting.proto", roots=["LightGet"])])

    assert 'import "hue/v1/common.proto";' in out["hue/v1/lighting.proto"]


def test_a_shared_dependency_is_emitted_once() -> None:
    out = build(
        [
            FileSpec("hue/v1/lighting.proto", roots=["LightGet"]),
            FileSpec("hue/v1/events.proto", roots=["Event"]),
        ]
    )
    whole = "".join(out.values())

    assert whole.count("message ResourceIdentifier {") == 1


def test_a_file_does_not_import_itself() -> None:
    out = build([FileSpec("hue/v1/common.proto", roots=["On"])])

    assert 'import "hue/v1/common.proto";' not in out["hue/v1/common.proto"]


def test_date_time_becomes_a_timestamp_and_pulls_in_its_import() -> None:
    spec = {
        "components": {
            "schemas": {
                "E": {
                    "type": "object",
                    "properties": {
                        "creationtime": {"type": "string", "format": "date-time"}
                    },
                }
            }
        }
    }
    rendered = generate_files(
        spec,
        [FileSpec("hue/v1/events.proto", roots=["E"])],
        package="hue.v1",
        default_file="hue/v1/common.proto",
    )
    out = rendered["hue/v1/events.proto"].render()

    assert "optional .google.protobuf.Timestamp creationtime = 1;" in out
    assert 'import "google/protobuf/timestamp.proto";' in out


def test_oneof_annotation_groups_named_fields() -> None:
    """Hue rejects setting both colour and colour temperature.

    Modelling that as a oneof makes the invalid request unrepresentable
    rather than something the gateway has to validate.
    """
    spec = {
        "components": {
            "schemas": {
                "LightPut": {
                    "type": "object",
                    "properties": {
                        "on": {"type": "boolean"},
                        "color": {"type": "string"},
                        "color_temperature": {"type": "string"},
                    },
                }
            }
        }
    }
    rendered = generate_files(
        spec,
        [
            FileSpec(
                "hue/v1/lighting.proto",
                roots=["LightPut"],
                oneofs={"LightPut": {"colour": ["color", "color_temperature"]}},
            )
        ],
        package="hue.v1",
        default_file="hue/v1/common.proto",
    )
    out = rendered["hue/v1/lighting.proto"].render()

    assert "oneof colour {" in out
    assert "optional bool on = 1;" in out
    # oneof members carry presence inherently, so no `optional` keyword.
    assert "optional string color =" not in out


def test_oneof_naming_an_unknown_field_is_rejected() -> None:
    spec = {
        "components": {
            "schemas": {
                "M": {"type": "object", "properties": {"a": {"type": "string"}}}
            }
        }
    }

    with pytest.raises(ValueError, match="nope"):
        generate_files(
            spec,
            [FileSpec("hue/v1/m.proto", roots=["M"], oneofs={"M": {"g": ["nope"]}})],
            package="hue.v1",
            default_file="hue/v1/common.proto",
        )


def _spec(schemas: dict[str, Any]) -> dict[str, Any]:
    return {"components": {"schemas": schemas}}


def _gen(spec: dict[str, Any], files: list[FileSpec], **kwargs: Any) -> dict[str, str]:
    rendered = generate_files(
        spec, files, package="hue.v1", default_file="hue/v1/common.proto", **kwargs
    )
    return {path: proto.render() for path, proto in rendered.items()}


def test_a_property_less_object_is_rejected_rather_than_emitted_empty() -> None:
    """An empty message can never carry a byte.

    Hue's ApiResponse.data is `array of {type: object}`; emitting it as an
    empty message silently discarded every REST payload.
    """
    spec = _spec({"M": {"type": "object", "properties": {"data": {"type": "object"}}}})

    with pytest.raises(ValueError, match=r"M\.Data"):
        _gen(spec, [FileSpec("hue/v1/m.proto", roots=["M"])])


def test_a_property_less_object_can_be_allowed_explicitly() -> None:
    """Some Hue objects really are empty: `identify` is a bare trigger."""
    spec = _spec(
        {"M": {"type": "object", "properties": {"identify": {"type": "object"}}}}
    )

    out = _gen(
        spec, [FileSpec("hue/v1/m.proto", roots=["M"])], allow_empty=["M.Identify"]
    )

    assert "message Identify {" in out["hue/v1/m.proto"]


def test_additional_properties_are_preserved_as_a_struct() -> None:
    """The event stream's payload lives in additionalProperties.

    Dropping it would leave the generated message able to describe which
    resource changed but not what changed about it.
    """
    spec = _spec(
        {
            "M": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "additionalProperties": True,
            }
        }
    )

    out = _gen(spec, [FileSpec("hue/v1/m.proto", roots=["M"])])

    assert (
        "optional .google.protobuf.Struct additional_properties ="
        in out["hue/v1/m.proto"]
    )
    assert 'import "google/protobuf/struct.proto";' in out["hue/v1/m.proto"]


def test_a_root_that_does_not_exist_is_rejected() -> None:
    """A typo would otherwise stop a file being generated, silently."""
    with pytest.raises(ValueError, match="Missing"):
        _gen(
            _spec({"M": {"type": "object", "properties": {}}}),
            [FileSpec("hue/v1/m.proto", roots=["Missing"])],
            allow_empty=["M"],
        )


def test_two_files_claiming_the_same_root_are_rejected() -> None:
    spec = _spec({"M": {"type": "object", "properties": {"a": {"type": "string"}}}})

    with pytest.raises(ValueError, match="claimed by"):
        _gen(
            spec,
            [
                FileSpec("hue/v1/a.proto", roots=["M"]),
                FileSpec("hue/v1/b.proto", roots=["M"]),
            ],
        )


def test_schemas_reached_only_through_flattened_allof_are_not_emitted() -> None:
    """Ownership must follow what the converter really references.

    A multi-member allOf is flattened and inlined, so its members are never
    referenced by name. Emitting them anyway produces dead messages that
    permanently consume field numbers in the lock file.
    """
    spec = _spec(
        {
            "M": {
                "allOf": [
                    {"$ref": "#/components/schemas/Base"},
                    {"type": "object", "properties": {"b": {"type": "string"}}},
                ]
            },
            "Base": {"type": "object", "properties": {"a": {"type": "string"}}},
        }
    )

    out = _gen(spec, [FileSpec("hue/v1/m.proto", roots=["M"])])
    whole = "".join(out.values())

    assert "message M {" in whole
    assert "message Base {" not in whole
