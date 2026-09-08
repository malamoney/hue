"""Splitting the generated schemas across several proto files.

Each file names the schemas it owns. Anything reached transitively that no
file claims goes to a default file, so shared building blocks like `On` and
`Dimming` do not have to be assigned by hand. A reference to a schema owned by
another file becomes an import; references are already fully qualified, so
nothing else changes.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from protogen.convert import REF, SCHEMA_REF_PREFIX, _Converter
from protogen.model import Message, OneOf, ProtoFile
from protogen.naming import pascal_case
from protogen.numbering import FieldNumbers

# message name -> oneof group name -> member field names
OneOfSpec = dict[str, dict[str, list[str]]]


@dataclass(frozen=True)
class FileSpec:
    """One output file: the schemas it owns, and any hand-annotated oneofs."""

    path: str
    roots: list[str]
    oneofs: OneOfSpec = field(default_factory=dict)


def _refs_in(node: Any) -> list[str]:
    """Every component `$ref` anywhere inside `node`."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if (
                key == REF
                and isinstance(value, str)
                and value.startswith(SCHEMA_REF_PREFIX)
            ):
                found.append(value[len(SCHEMA_REF_PREFIX) :])
            else:
                found.extend(_refs_in(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_refs_in(item))
    return found


def _reachable(spec: dict[str, Any], roots: list[str]) -> set[str]:
    schemas = spec.get("components", {}).get("schemas", {})
    seen: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in seen or name not in schemas:
            continue
        seen.add(name)
        queue.extend(_refs_in(schemas[name]))
    return seen


def plan_ownership(
    spec: dict[str, Any], files: list[FileSpec], default_file: str
) -> dict[str, str]:
    """Map each schema's protobuf name to the file that will declare it."""
    owner_of: dict[str, str] = {}
    for file_spec in files:
        for root in file_spec.roots:
            owner_of[pascal_case(root)] = file_spec.path

    all_roots = [root for file_spec in files for root in file_spec.roots]
    for name in sorted(_reachable(spec, all_roots)):
        owner_of.setdefault(pascal_case(name), default_file)
    return owner_of


def _apply_oneofs(message: Message, groups: dict[str, list[str]]) -> Message:
    by_name = {f.name: f for f in message.fields}
    grouped: list[OneOf] = []
    claimed: set[str] = set()

    for group_name, member_names in groups.items():
        members = []
        for member_name in member_names:
            member = by_name.get(member_name)
            if member is None:
                raise ValueError(
                    f"{message.name}: oneof {group_name!r} names unknown field "
                    f"{member_name!r}"
                )
            if member.repeated:
                raise ValueError(
                    f"{message.name}: oneof {group_name!r} cannot contain the "
                    f"repeated field {member_name!r}"
                )
            members.append(member)
            claimed.add(member_name)
        grouped.append(OneOf(name=group_name, fields=tuple(members)))

    remaining = tuple(f for f in message.fields if f.name not in claimed)
    return Message(
        name=message.name,
        fields=remaining,
        oneofs=tuple(grouped),
        enums=message.enums,
        messages=message.messages,
        comment=message.comment,
    )


def generate_files(
    spec: dict[str, Any],
    files: list[FileSpec],
    *,
    package: str,
    default_file: str,
    numbers: FieldNumbers | None = None,
    header: str | None = None,
) -> dict[str, ProtoFile]:
    """Generate every file described by `files`, plus the default file."""
    numbers = numbers or FieldNumbers()
    owner_of = plan_ownership(spec, files, default_file)

    by_path = {file_spec.path: file_spec for file_spec in files}
    by_path.setdefault(default_file, FileSpec(default_file, roots=[]))

    # Each file emits exactly the schemas it owns.
    owned: dict[str, list[str]] = {path: [] for path in by_path}
    schemas = spec.get("components", {}).get("schemas", {})
    for schema_name in schemas:
        owner = owner_of.get(pascal_case(schema_name))
        if owner in owned:
            owned[owner].append(schema_name)

    generated: dict[str, ProtoFile] = {}
    for path, file_spec in by_path.items():
        converter = _Converter(spec, package, numbers, owner_of, path)
        messages, enums = converter.convert(sorted(owned[path]))
        if not messages and not enums:
            continue

        messages = [
            _apply_oneofs(message, file_spec.oneofs[message.name])
            if message.name in file_spec.oneofs
            else message
            for message in messages
        ]
        generated[path] = ProtoFile(
            package=package,
            messages=tuple(messages),
            enums=tuple(enums),
            imports=tuple(sorted(converter.imports)),
            header=header,
        )

    _check_oneof_targets(by_path, generated)
    return generated


def _check_oneof_targets(
    by_path: dict[str, FileSpec], generated: dict[str, ProtoFile]
) -> None:
    """A oneof naming a message that was never generated is a silent no-op."""
    for path, file_spec in by_path.items():
        produced = {m.name for m in generated.get(path, ProtoFile(package="")).messages}
        for message_name in file_spec.oneofs:
            if message_name not in produced:
                raise ValueError(
                    f"{path}: oneof declared for {message_name!r}, which this "
                    "file does not generate"
                )


@dataclass(frozen=True)
class Manifest:
    """Everything a generation run needs, read from a TOML file."""

    spec: str
    out_dir: str
    package: str
    default_file: str
    numbers: str
    files: list[FileSpec]


def load_manifest(path: Path) -> Manifest:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    files = [
        FileSpec(
            path=file_path,
            roots=list(entry.get("roots", [])),
            oneofs={
                message: {group: list(members) for group, members in groups.items()}
                for message, groups in entry.get("oneofs", {}).items()
            },
        )
        for file_path, entry in raw.get("files", {}).items()
    ]

    return Manifest(
        spec=raw["spec"],
        out_dir=raw["out_dir"],
        package=raw["package"],
        default_file=raw["default_file"],
        numbers=raw["numbers"],
        files=files,
    )
