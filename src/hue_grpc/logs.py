"""Structured logs, and the correlation ID that ties one RPC's lines together.

A single RPC produces log lines from several layers: the interceptor's own
record, whatever the service logs, and whatever the Hue transport logs while
talking to the Bridge. Passing a correlation ID down through every signature
would mean every layer taking an argument it does not otherwise care about, so
it lives in a context variable instead and the formatter reads it back out.

The same context carries the upstream HTTP status back *up*: `hue_grpc.hue`
learns it several frames below the interceptor that writes the RPC's line, and
must not have to know that an RPC is what it is serving. That is why this
module sits at the top level rather than under `hue_grpc.serving` — both sides
use it, and neither may import the other.

Never logged, here or anywhere: the Application Key, the Gateway Token, or
request metadata wholesale. Records are built from named fields for exactly
that reason — there is no path by which a header ends up in one by accident.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "Call",
    "JsonFormatter",
    "TextFormatter",
    "configure_logging",
    "correlation_id",
    "current_call",
    "fields",
    "new_correlation_id",
    "record_upstream_status",
    "tracking_call",
]

#: Marks the handler `configure_logging` installs, so calling it again
#: replaces that handler instead of adding a second one.
_HANDLER_MARK = "_hue_grpc_handler"

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


@dataclass
class Call:
    """What is known about the RPC currently being served.

    Mutable and deliberately so: the correlation ID is fixed when the call
    starts, and the upstream status is filled in later by whichever layer
    reaches the Bridge.
    """

    correlation_id: str
    upstream_status: int | None = None


_call: ContextVar[Call | None] = ContextVar("hue_grpc_call", default=None)


def new_correlation_id() -> str:
    """An ID for a call that arrived without one. Alphanumeric, so it survives
    being pasted into a URL, a journalctl filter or a shell."""
    return uuid.uuid4().hex


@contextmanager
def tracking_call(correlation_id: str) -> Iterator[Call]:
    """Make `correlation_id` the one every record logged inside this carries."""
    call = Call(correlation_id=correlation_id)
    token = _call.set(call)
    try:
        yield call
    finally:
        _call.reset(token)


def current_call() -> Call | None:
    """The RPC being served on this task, or `None` outside one."""
    return _call.get()


def correlation_id() -> str | None:
    call = _call.get()
    return None if call is None else call.correlation_id


def record_upstream_status(status: int) -> None:
    """Note the Bridge's HTTP status on the RPC's log line.

    Outside an RPC — a Bridge call made at startup, say — there is nothing to
    note it on, and that is not an error.
    """
    call = _call.get()
    if call is not None:
        call.upstream_status = status


def fields(**values: Any) -> dict[str, Any]:
    """Named fields for one record: `log.info("finished", **fields(rpc=...))`.

    Named, rather than interpolated into the message, so that a log consumer
    can filter on them and so that nothing unnamed can slip in.
    """
    return {"extra": {name: value for name, value in values.items()}}


def _record_fields(record: logging.LogRecord) -> dict[str, Any]:
    """A record's structured fields: what `fields()` attached, plus the call's.

    `logging` flattens `extra` onto the record, so the fields are found by
    subtracting everything a bare record already has.
    """
    attached = {
        name: value
        for name, value in record.__dict__.items()
        if name not in _RESERVED and not name.startswith("_")
    }
    call = _call.get()
    if call is not None:
        attached = {"correlation_id": call.correlation_id, **attached}
    return attached


class JsonFormatter(logging.Formatter):
    """One JSON object per line, which is what journald and `jq` both want."""

    def format(self, record: logging.LogRecord) -> str:
        document: dict[str, Any] = {
            "time": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_record_fields(record),
        }
        if record.exc_info is not None:
            document["exception"] = self.formatException(record.exc_info)
        # `default=str` rather than a failure: a log line is not the place to
        # discover that something was not serialisable.
        return json.dumps(document, default=str)


class TextFormatter(logging.Formatter):
    """The same fields, for a person watching a terminal."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)s %(message)s",
            datefmt="%H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        attached = " ".join(
            f"{name}={value}" for name, value in _record_fields(record).items()
        )
        return f"{rendered} {attached}" if attached else rendered


def configure_logging(
    *, level: str = "INFO", style: Literal["json", "text"] = "json"
) -> None:
    """Send this process's logs to stderr, structured, once.

    Idempotent: a second call replaces the handler the first installed rather
    than adding another, so nothing is ever logged twice.
    """
    root = logging.getLogger()
    for existing in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(existing)
        existing.close()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if style == "json" else TextFormatter())
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)
    root.setLevel(level.upper())
