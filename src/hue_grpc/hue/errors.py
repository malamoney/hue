"""What the Bridge said when it refused, in the Bridge's own words.

The Bridge speaks two error dialects. CLIP v2 answers with an `errors` array
alongside its `data`; the v1 API — where Pairing lives — answers with a list
of entries each holding one `error` object. They disagree about the shape and
agree about the field that matters: `description`, written for a person and
naming the thing Hue objected to, which is more than a status code can say.

Nothing here raises. This is read on a path that is already failing, and a
Bridge that answered a failure with HTML, with an empty body, or with an
`errors` array full of something unexpected must not become a second,
unrelated failure on the way to the client.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["descriptions"]


def descriptions(payload: Any) -> list[str]:
    """Every description in `payload`, in the order the Bridge gave them.

    Empty when the payload holds none, which includes the payload not being
    an error envelope at all.
    """
    entries = payload if isinstance(payload, list) else [payload]
    said: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        # CLIP v2 keeps them in an array; v1 keeps one per entry. A payload is
        # only ever one dialect, so reading for both costs nothing.
        said.extend(_described(entry.get("errors")))
        said.extend(_described([entry.get("error")]))
    return said


def _described(errors: Any) -> list[str]:
    """The descriptions among `errors`, skipping whatever is not one."""
    if not isinstance(errors, list):
        return []
    return [
        error["description"].strip()
        for error in errors
        if isinstance(error, Mapping)
        and isinstance(error.get("description"), str)
        and error["description"].strip()
    ]
