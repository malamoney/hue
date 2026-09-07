"""The protobuf intermediate model, and rendering it to `.proto` source.

The invariants that matter are enforced in `__post_init__` rather than at
render time, so an invalid combination cannot be constructed at all:

* a field is never both `optional` and `repeated` (proto3 rejects it);
* field numbers are unique within a message, counting `oneof` members;
* every enum has a zero value.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field

INDENT = "  "


def _indent(text: str, depth: int) -> str:
    return f"{INDENT * depth}{text}"


@dataclass(frozen=True)
class Field:
    name: str
    type_name: str
    number: int
    optional: bool = False
    repeated: bool = False
    comment: str | None = None

    def __post_init__(self) -> None:
        if self.optional and self.repeated:
            raise ValueError(
                f"{self.name}: proto3 has no optional repeated field; "
                "repeated fields already distinguish empty from absent"
            )
        if self.number < 1:
            raise ValueError(f"{self.name}: field number must be positive")

    def render(self, depth: int, *, in_oneof: bool = False) -> list[str]:
        lines: list[str] = []
        if self.comment:
            lines.extend(
                _indent(f"// {line}", depth) for line in self.comment.splitlines()
            )

        prefix = ""
        if self.repeated:
            prefix = "repeated "
        elif self.optional and not in_oneof:
            # oneof members already carry explicit presence.
            prefix = "optional "

        lines.append(
            _indent(f"{prefix}{self.type_name} {self.name} = {self.number};", depth)
        )
        return lines


@dataclass(frozen=True)
class OneOf:
    name: str
    fields: tuple[Field, ...]
    comment: str | None = None

    def render(self, depth: int) -> list[str]:
        lines: list[str] = []
        if self.comment:
            lines.extend(
                _indent(f"// {line}", depth) for line in self.comment.splitlines()
            )
        lines.append(_indent(f"oneof {self.name} {{", depth))
        for member in self.fields:
            lines.extend(member.render(depth + 1, in_oneof=True))
        lines.append(_indent("}", depth))
        return lines


@dataclass(frozen=True)
class EnumValue:
    name: str
    number: int


@dataclass(frozen=True)
class Enum:
    name: str
    values: tuple[EnumValue, ...]
    comment: str | None = None

    def __post_init__(self) -> None:
        if not any(value.number == 0 for value in self.values):
            raise ValueError(f"{self.name}: enum needs a zero value (UNSPECIFIED)")

        names = [value.name for value in self.values]
        if len(set(names)) != len(names):
            clashing = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"{self.name}: duplicate enum value name(s) {clashing}")

        numbers = [value.number for value in self.values]
        if len(set(numbers)) != len(numbers):
            clashing_numbers = sorted({n for n in numbers if numbers.count(n) > 1})
            raise ValueError(
                f"{self.name}: duplicate enum number(s) {clashing_numbers}"
            )

    def render(self, depth: int) -> list[str]:
        lines: list[str] = []
        if self.comment:
            lines.extend(
                _indent(f"// {line}", depth) for line in self.comment.splitlines()
            )
        lines.append(_indent(f"enum {self.name} {{", depth))
        for value in self.values:
            lines.append(_indent(f"{value.name} = {value.number};", depth + 1))
        lines.append(_indent("}", depth))
        return lines


@dataclass(frozen=True)
class Message:
    name: str
    fields: tuple[Field, ...] = ()
    oneofs: tuple[OneOf, ...] = ()
    enums: tuple[Enum, ...] = ()
    messages: tuple[Message, ...] = ()
    comment: str | None = None

    def __post_init__(self) -> None:
        numbers = [f.number for f in self.fields]
        numbers.extend(f.number for group in self.oneofs for f in group.fields)
        duplicates = {n for n in numbers if numbers.count(n) > 1}
        if duplicates:
            raise ValueError(
                f"{self.name}: duplicate field number(s) {sorted(duplicates)}"
            )

    def render(self, depth: int = 0) -> list[str]:
        lines: list[str] = []
        if self.comment:
            lines.extend(
                _indent(f"// {line}", depth) for line in self.comment.splitlines()
            )
        lines.append(_indent(f"message {self.name} {{", depth))

        for enum in self.enums:
            lines.extend(enum.render(depth + 1))
        for nested in self.messages:
            lines.extend(nested.render(depth + 1))
        for member in self.fields:
            lines.extend(member.render(depth + 1))
        for group in self.oneofs:
            lines.extend(group.render(depth + 1))

        lines.append(_indent("}", depth))
        return lines


@dataclass(frozen=True)
class ProtoFile:
    package: str
    messages: tuple[Message, ...] = ()
    enums: tuple[Enum, ...] = ()
    imports: tuple[str, ...] = ()
    header: str | None = None
    options: tuple[tuple[str, str], ...] = dataclass_field(default_factory=tuple)

    def render(self) -> str:
        lines: list[str] = []
        if self.header:
            lines.extend(f"// {line}" for line in self.header.splitlines())
            lines.append("")

        lines.append('syntax = "proto3";')
        lines.append("")
        lines.append(f"package {self.package};")

        if self.imports:
            lines.append("")
            lines.extend(f'import "{path}";' for path in sorted(self.imports))
        if self.options:
            lines.append("")
            lines.extend(f"option {name} = {value};" for name, value in self.options)

        for enum in self.enums:
            lines.append("")
            lines.extend(enum.render(0))
        for message in self.messages:
            lines.append("")
            lines.extend(message.render(0))

        return "\n".join(lines) + "\n"
