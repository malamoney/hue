"""Stable protobuf field numbers across spec revisions.

Field numbers are the wire format. If they were derived from the order of
properties in `openapi.yaml`, inserting one property upstream would shift
every number after it, and a client built against the old definitions would
decode the new bytes as a different field entirely — silently, with no error
anywhere.

So numbers live in a committed JSON file keyed by `Message.field`, using the
message's full nested path. An assignment, once made, is never changed, and a
number belonging to a field that has since disappeared is never handed out
again: old and new clients would otherwise disagree about what it means.
"""

from __future__ import annotations

import json
from pathlib import Path


class FieldNumbers:
    """A `Message.field` -> number map that only ever grows."""

    def __init__(self, assignments: dict[str, int] | None = None) -> None:
        self._assignments: dict[str, int] = dict(assignments or {})

    @classmethod
    def load(cls, path: Path) -> FieldNumbers:
        """Load assignments from `path`, or start empty if it does not exist."""
        if not path.exists():
            return cls()

        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"{path}: expected an object of field assignments")
        return cls({str(key): int(value) for key, value in loaded.items()})

    def assign(self, scope: str, field_name: str) -> int:
        """Return the number for `scope.field_name`, allocating one if needed.

        `scope` is the message's full nested path, so a field on
        `LightGet.Powerup` cannot collide with one on `LightGet`.
        """
        key = f"{scope}.{field_name}"
        existing = self._assignments.get(key)
        if existing is not None:
            return existing

        number = self._next_free(scope)
        self._assignments[key] = number
        return number

    def _next_free(self, scope: str) -> int:
        prefix = f"{scope}."
        used = [
            number
            for key, number in self._assignments.items()
            # Only this message's own fields: `LightGet.Powerup.preset` is not
            # a field of `LightGet`.
            if key.startswith(prefix) and "." not in key[len(prefix) :]
        ]
        # max + 1 rather than the lowest gap, so a retired number stays retired.
        return max(used, default=0) + 1

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        serialised = json.dumps(
            dict(sorted(self._assignments.items())), indent=2, sort_keys=True
        )
        path.write_text(f"{serialised}\n", encoding="utf-8")

    def as_dict(self) -> dict[str, int]:
        return dict(self._assignments)
