"""Tests for loading and normalising the OpenAPI document."""

from __future__ import annotations

from pathlib import Path

import pytest
from protogen.spec import flatten_schema, load_spec, resolve_ref


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "spec.yaml"
    path.write_text(text)
    return path


def test_load_spec_parses_yaml(tmp_path: Path) -> None:
    spec = load_spec(write(tmp_path, "openapi: 3.0.3\npaths: {}\n"))

    assert spec["openapi"] == "3.0.3"


def test_unquoted_on_stays_a_string_key(tmp_path: Path) -> None:
    """YAML 1.1 resolves bare `on` to True, which would silently drop the
    single most important field in the Hue API."""
    spec = load_spec(write(tmp_path, "properties:\n  on: {type: boolean}\n  off: {}\n"))

    assert set(spec["properties"]) == {"on", "off"}


def test_bare_booleans_still_parse_as_booleans(tmp_path: Path) -> None:
    """Only the yes/no/on/off spellings are disabled, not true/false."""
    spec = load_spec(write(tmp_path, "a: true\nb: false\n"))

    assert spec == {"a": True, "b": False}


def test_resolve_ref_returns_the_target(tmp_path: Path) -> None:
    spec = load_spec(
        write(tmp_path, "components:\n  schemas:\n    Foo: {type: object}\n")
    )

    assert resolve_ref(spec, "#/components/schemas/Foo") == {"type": "object"}


def test_resolve_ref_rejects_unknown_target(tmp_path: Path) -> None:
    spec = load_spec(write(tmp_path, "components: {schemas: {}}\n"))

    with pytest.raises(KeyError, match="Nope"):
        resolve_ref(spec, "#/components/schemas/Nope")


def test_resolve_ref_rejects_external_references(tmp_path: Path) -> None:
    spec = load_spec(write(tmp_path, "components: {schemas: {}}\n"))

    with pytest.raises(ValueError, match="external"):
        resolve_ref(spec, "https://example.com/other.yaml#/Foo")


def test_flatten_merges_allof_members(tmp_path: Path) -> None:
    spec = load_spec(
        write(
            tmp_path,
            """
            components:
              schemas:
                Base: {type: object, properties: {a: {type: string}}}
            """,
        )
    )
    schema = {
        "allOf": [
            {"$ref": "#/components/schemas/Base"},
            {"type": "object", "properties": {"b": {"type": "integer"}}},
        ]
    }

    assert set(flatten_schema(spec, schema)["properties"]) == {"a", "b"}


def test_flatten_unions_required_across_members(tmp_path: Path) -> None:
    spec = load_spec(write(tmp_path, "components: {schemas: {}}\n"))
    schema = {
        "allOf": [
            {"required": ["a"], "properties": {"a": {"type": "string"}}},
            {"required": ["b"], "properties": {"b": {"type": "string"}}},
        ]
    }

    assert sorted(flatten_schema(spec, schema)["required"]) == ["a", "b"]


def test_flatten_leaves_a_plain_schema_alone(tmp_path: Path) -> None:
    spec = load_spec(write(tmp_path, "components: {schemas: {}}\n"))
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}

    assert flatten_schema(spec, schema) == schema


def test_flatten_resolves_a_top_level_ref(tmp_path: Path) -> None:
    spec = load_spec(
        write(tmp_path, "components:\n  schemas:\n    Foo: {type: string}\n")
    )

    assert flatten_schema(spec, {"$ref": "#/components/schemas/Foo"}) == {
        "type": "string"
    }
