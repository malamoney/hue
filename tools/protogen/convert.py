"""Turns OpenAPI schemas into the protobuf model.

The rule this module exists to enforce is in `_convert_property`: a property
that is not listed in its schema's `required` array becomes an `optional`
protobuf field. Without it, an omitted `on` and an explicit `on: false` are
the same bytes on the wire, and the gateway turns lights off when asked to
leave them alone. See docs/adr/0001.
"""

from __future__ import annotations

from typing import Any

from protogen.model import Enum, EnumValue, Field, Message, ProtoFile
from protogen.naming import enum_value_name, pascal_case
from protogen.spec import flatten_schema

REF = "$ref"
SCHEMA_REF_PREFIX = "#/components/schemas/"

# OpenAPI (type, format) -> protobuf scalar. Absent formats take the default.
_SCALARS: dict[tuple[str, str | None], str] = {
    ("string", None): "string",
    ("boolean", None): "bool",
    ("integer", None): "int32",
    ("integer", "int32"): "int32",
    ("integer", "int64"): "int64",
    ("number", None): "double",
    ("number", "double"): "double",
    ("number", "float"): "float",
}


class _Converter:
    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec
        self._emitted: dict[str, Message] = {}
        self._pending: list[str] = []

    def convert(self, roots: list[str]) -> list[Message]:
        self._pending.extend(roots)
        while self._pending:
            name = self._pending.pop(0)
            if name in self._emitted:
                continue
            schema = flatten_schema(self._spec, {REF: f"{SCHEMA_REF_PREFIX}{name}"})
            # Reserve the name first so a self-reference does not requeue it.
            self._emitted[name] = Message(name=pascal_case(name))
            self._emitted[name] = self._message(pascal_case(name), schema)
        return list(self._emitted.values())

    def _message(self, name: str, schema: dict[str, Any]) -> Message:
        required = set(schema.get("required", []))
        fields: list[Field] = []
        enums: list[Enum] = []
        nested: list[Message] = []

        for number, (prop_name, prop_schema) in enumerate(
            schema.get("properties", {}).items(), start=1
        ):
            field = self._convert_property(
                prop_name,
                prop_schema,
                number=number,
                required=prop_name in required,
                enums=enums,
                nested=nested,
            )
            fields.append(field)

        return Message(
            name=name,
            fields=tuple(fields),
            enums=tuple(enums),
            messages=tuple(nested),
            comment=schema.get("description"),
        )

    def _convert_property(
        self,
        name: str,
        schema: dict[str, Any],
        *,
        number: int,
        required: bool,
        enums: list[Enum],
        nested: list[Message],
    ) -> Field:
        if schema.get("type") == "array":
            item_type = self._type_of(name, schema.get("items", {}), enums, nested)
            # Never optional: proto3 rejects it, and an empty list already
            # means "absent" for a repeated field.
            return Field(name, item_type, number, repeated=True)

        type_name = self._type_of(name, schema, enums, nested)
        return Field(name, type_name, number, optional=not required)

    def _type_of(
        self,
        name: str,
        schema: dict[str, Any],
        enums: list[Enum],
        nested: list[Message],
    ) -> str:
        if REF in schema:
            ref = schema[REF]
            if not ref.startswith(SCHEMA_REF_PREFIX):
                raise ValueError(f"{name}: unsupported $ref target {ref}")
            target = ref[len(SCHEMA_REF_PREFIX) :]
            self._pending.append(target)
            return pascal_case(target)

        if "enum" in schema:
            enum = _build_enum(name, schema["enum"])
            enums.append(enum)
            return enum.name

        schema_type = schema.get("type")
        if schema_type == "object" or (schema_type is None and "properties" in schema):
            message = self._message(
                pascal_case(name), flatten_schema(self._spec, schema)
            )
            nested.append(message)
            return message.name

        scalar = _SCALARS.get((str(schema_type), schema.get("format")))
        if scalar is None:
            scalar = _SCALARS.get((str(schema_type), None))
        if scalar is None:
            raise ValueError(
                f"{name}: unsupported schema type {schema_type!r} "
                f"(format {schema.get('format')!r})"
            )
        return scalar


def _build_enum(name: str, values: list[str]) -> Enum:
    enum_name = pascal_case(name)
    members = [EnumValue(enum_value_name(enum_name, "unspecified"), 0)]
    members.extend(
        EnumValue(enum_value_name(enum_name, value), number)
        for number, value in enumerate(values, start=1)
    )
    return Enum(name=enum_name, values=tuple(members))


def convert_document(
    spec: dict[str, Any],
    roots: list[str],
    *,
    package: str,
    header: str | None = None,
) -> ProtoFile:
    """Convert `roots` and everything they reference into one proto file."""
    messages = _Converter(spec).convert(roots)
    return ProtoFile(package=package, messages=tuple(messages), header=header)
