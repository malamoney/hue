"""Loading and normalising the OpenAPI document.

Two things happen here that the rest of the generator then does not have to
think about: `$ref` indirection and `allOf` composition. The Hue spec uses 175
`allOf` members, so flattening them once at the edge keeps the conversion
layer dealing only in plain schemas.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

LOCAL_REF_PREFIX = "#/"


class _SpecLoader(yaml.SafeLoader):  # type: ignore[misc]
    """SafeLoader that does not treat `on`/`off`/`yes`/`no` as booleans.

    YAML 1.1 resolves bare `on` to True. The Hue spec quotes its `"on"` keys
    today, but if that ever changes the light power field would silently
    become a boolean key and vanish from the generated protobuf rather than
    failing loudly. `true` and `false` still resolve normally, and `null`
    keeps its resolver even though it shares a first letter with `no`.
    """


# The empty-string key exists for empty scalars, so this must be a set
# membership test: `"" in "oOyYnN"` is True and would widen the filter.
_BOOL_WORD_INITIALS = frozenset("oOyYnN")

# Implicit resolvers are keyed by the token's first character, so dropping the
# bool resolver for o/y/n leaves true/false (t/f) untouched.
_SpecLoader.yaml_implicit_resolvers = {
    char: [
        (tag, regexp)
        for tag, regexp in resolvers
        if not (char in _BOOL_WORD_INITIALS and tag == "tag:yaml.org,2002:bool")
    ]
    for char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def load_spec(path: Path) -> dict[str, Any]:
    """Parse the OpenAPI document at `path`."""
    with path.open(encoding="utf-8") as handle:
        loaded = yaml.load(handle, Loader=_SpecLoader)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: expected a mapping at the document root")
    return loaded


def resolve_ref(spec: dict[str, Any], ref: str) -> dict[str, Any]:
    """Resolve a local `$ref` pointer against `spec`."""
    if not ref.startswith(LOCAL_REF_PREFIX):
        raise ValueError(f"external $ref is not supported: {ref}")

    node: Any = spec
    for part in ref[len(LOCAL_REF_PREFIX) :].split("/"):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"$ref target not found: {ref} (missing {part!r})")
        node = node[part]

    if not isinstance(node, dict):
        raise ValueError(f"$ref target is not a schema: {ref}")
    return node


def flatten_schema(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Return `schema` with any top-level `$ref` and `allOf` composition resolved.

    Nested schemas are left alone: they are flattened in turn as the converter
    descends into them.
    """
    if "$ref" in schema:
        return flatten_schema(spec, resolve_ref(spec, schema["$ref"]))

    if "allOf" not in schema:
        return schema

    merged: dict[str, Any] = {
        key: value for key, value in schema.items() if key != "allOf"
    }
    properties: dict[str, Any] = dict(merged.get("properties", {}))
    required: list[str] = list(merged.get("required", []))

    for member in schema["allOf"]:
        flat = flatten_schema(spec, member)
        properties.update(flat.get("properties", {}))
        required.extend(
            name for name in flat.get("required", []) if name not in required
        )
        for key, value in flat.items():
            if key not in {"properties", "required"} and key not in merged:
                merged[key] = value

    if properties:
        merged["properties"] = properties
    if required:
        merged["required"] = required
    return merged
