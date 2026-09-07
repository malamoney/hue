"""Turns OpenAPI schemas into the protobuf model.

The rule this module exists to enforce is in `_convert_property`: a property
that is not listed in its schema's `required` array becomes an `optional`
protobuf field. Without it, an omitted `on` and an explicit `on: false` are
the same bytes on the wire, and the gateway turns lights off when asked to
leave them alone. See docs/adr/0001.

Two subtleties beyond that:

* A `$ref` target is not always a message. Hue has component schemas that are
  bare scalars (`Brightness` is a number) or bare enums (`LightArchetype`).
  Emitting a fieldless message for those would silently discard the value.
* References to component schemas are emitted fully qualified. Protobuf
  resolves names innermost-first, so an inline object named `On` nested in the
  referring message would otherwise capture a reference meant for the
  top-level `On`, leaving the field self-recursive and the real value
  unreachable.
"""

from __future__ import annotations

from typing import Any, Literal

from protogen.model import Enum, EnumValue, Field, Message, ProtoFile
from protogen.naming import enum_value_name, pascal_case
from protogen.numbering import FieldNumbers
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

Kind = Literal["enum", "message", "scalar"]


def _single_ref(schema: dict[str, Any]) -> str | None:
    """Return the `$ref` this schema stands for, if it is purely a reference.

    Covers the common OpenAPI idiom of wrapping a reference in `allOf` purely
    to attach a description: `{allOf: [{$ref: X}], description: "..."}`.
    Flattening those outright would inline the target and lose the reference.
    """
    if REF in schema:
        return str(schema[REF])

    members = schema.get("allOf")
    if (
        isinstance(members, list)
        and len(members) == 1
        and isinstance(members[0], dict)
        and set(members[0]) == {REF}
        and "properties" not in schema
        and "required" not in schema
    ):
        return str(members[0][REF])

    return None


def _classify(schema: dict[str, Any]) -> Kind:
    if "enum" in schema:
        return "enum"
    if schema.get("type") == "object" or "properties" in schema:
        return "message"
    return "scalar"


def _scalar_type(name: str, schema: dict[str, Any]) -> str:
    schema_type = str(schema.get("type"))
    scalar = _SCALARS.get((schema_type, schema.get("format"))) or _SCALARS.get(
        (schema_type, None)
    )
    if scalar is None:
        raise ValueError(
            f"{name}: unsupported schema type {schema.get('type')!r} "
            f"(format {schema.get('format')!r})"
        )
    return scalar


def _build_enum(name: str, values: list[Any]) -> Enum:
    non_strings = [value for value in values if not isinstance(value, str)]
    if non_strings:
        raise ValueError(
            f"{name}: non-string enum values are not supported: {non_strings}"
        )

    members = [EnumValue(enum_value_name(name, "unspecified"), 0)]
    members.extend(
        EnumValue(enum_value_name(name, value), number)
        for number, value in enumerate(values, start=1)
    )
    # Enum validates uniqueness: two source values can normalise to one name.
    return Enum(name=name, values=tuple(members))


class _Converter:
    def __init__(
        self, spec: dict[str, Any], package: str, numbers: FieldNumbers
    ) -> None:
        self._spec = spec
        self._package = package
        self._numbers = numbers
        self._messages: dict[str, Message] = {}
        self._enums: dict[str, Enum] = {}
        self._pending: list[str] = []

    def convert(self, roots: list[str]) -> tuple[list[Message], list[Enum]]:
        self._pending.extend(roots)
        while self._pending:
            self._emit_component(self._pending.pop(0))
        return list(self._messages.values()), list(self._enums.values())

    def _emit_component(self, schema_name: str) -> None:
        proto_name = pascal_case(schema_name)
        if proto_name in self._messages or proto_name in self._enums:
            return

        schema = flatten_schema(self._spec, {REF: f"{SCHEMA_REF_PREFIX}{schema_name}"})
        kind = _classify(schema)
        if kind == "enum":
            self._enums[proto_name] = _build_enum(proto_name, schema["enum"])
        elif kind == "message":
            self._messages[proto_name] = self._message(
                proto_name, schema, scope=proto_name
            )
        # A scalar component carries no declaration: it is inlined at each use.

    def _message(self, name: str, schema: dict[str, Any], *, scope: str) -> Message:
        required = set(schema.get("required", []))
        fields: list[Field] = []
        enums: list[Enum] = []
        nested: list[Message] = []

        for prop_name, prop_schema in schema.get("properties", {}).items():
            fields.append(
                self._convert_property(
                    prop_name,
                    prop_schema,
                    # Numbers come from the committed lock file, never from
                    # the order properties happen to appear in the spec.
                    number=self._numbers.assign(scope, prop_name),
                    required=prop_name in required,
                    enums=enums,
                    nested=nested,
                    scope=scope,
                )
            )

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
        scope: str,
    ) -> Field:
        if _single_ref(schema) is None:
            # Only flatten when this is not a reference: flattening resolves
            # `$ref` away, and the reference is what we need to preserve.
            schema = flatten_schema(self._spec, schema)

        if schema.get("type") == "array":
            item_type = self._type_of(
                name, schema.get("items", {}), enums, nested, scope
            )
            # Never optional: proto3 rejects it, and an empty list already
            # means "absent" for a repeated field.
            return Field(name, item_type, number, repeated=True)

        return Field(
            name,
            self._type_of(name, schema, enums, nested, scope),
            number,
            optional=not required,
        )

    def _type_of(
        self,
        name: str,
        schema: dict[str, Any],
        enums: list[Enum],
        nested: list[Message],
        scope: str,
    ) -> str:
        ref = _single_ref(schema)
        if ref is not None:
            return self._reference_type(name, ref)

        schema = flatten_schema(self._spec, schema)
        kind = _classify(schema)
        if kind == "enum":
            enum = _build_enum(pascal_case(name), schema["enum"])
            enums.append(enum)
            return enum.name
        if kind == "message":
            nested_name = pascal_case(name)
            message = self._message(nested_name, schema, scope=f"{scope}.{nested_name}")
            nested.append(message)
            return message.name
        return _scalar_type(name, schema)

    def _reference_type(self, name: str, ref: str) -> str:
        if not ref.startswith(SCHEMA_REF_PREFIX):
            raise ValueError(f"{name}: unsupported $ref target {ref}")

        target = ref[len(SCHEMA_REF_PREFIX) :]
        schema = flatten_schema(self._spec, {REF: ref})
        kind = _classify(schema)

        if kind == "scalar":
            # e.g. Brightness is a bare number; a fieldless message would
            # silently drop the value.
            return _scalar_type(name, schema)

        self._pending.append(target)
        # Fully qualified: a nested message of the same name would otherwise
        # capture this reference.
        return f".{self._package}.{pascal_case(target)}"


def convert_document(
    spec: dict[str, Any],
    roots: list[str],
    *,
    package: str,
    header: str | None = None,
    numbers: FieldNumbers | None = None,
) -> ProtoFile:
    """Convert `roots` and everything they reference into one proto file."""
    messages, enums = _Converter(spec, package, numbers or FieldNumbers()).convert(
        roots
    )
    return ProtoFile(
        package=package,
        messages=tuple(messages),
        enums=tuple(enums),
        header=header,
    )
