"""Structured logs, and the correlation ID that threads through a call.

The interceptor writes one record per RPC, but everything logged underneath it
— the Hue transport included — has to carry the same correlation ID, or the
line saying an RPC failed cannot be joined to the line saying why.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from hue_grpc.logs import (
    JsonFormatter,
    configure_logging,
    current_call,
    fields,
    new_correlation_id,
    record_upstream_status,
    tracking_call,
)


def render(record: logging.LogRecord) -> dict[str, Any]:
    parsed = json.loads(JsonFormatter().format(record))
    assert isinstance(parsed, dict)
    return parsed


def a_record(message: str = "something happened", **extra: Any) -> logging.LogRecord:
    return logging.LogRecord(
        name="hue_grpc.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
        **extra,
    )


def test_a_record_renders_as_one_json_object_per_line() -> None:
    line = JsonFormatter().format(a_record())

    assert "\n" not in line
    rendered = json.loads(line)
    assert rendered["message"] == "something happened"
    assert rendered["level"] == "INFO"
    assert rendered["logger"] == "hue_grpc.test"
    assert rendered["time"].endswith("+00:00")


def test_structured_fields_are_named_keys_not_a_formatted_string() -> None:
    record = a_record()
    for name, value in fields(rpc="/hue.v1.Test/Echo", status="OK")["extra"].items():
        setattr(record, name, value)

    rendered = render(record)

    assert rendered["rpc"] == "/hue.v1.Test/Echo"
    assert rendered["status"] == "OK"


def test_a_value_json_cannot_hold_is_rendered_rather_than_dropped() -> None:
    record = a_record()
    record.path = object()

    assert "object object at" in render(record)["path"]


def test_the_correlation_id_reaches_a_record_logged_anywhere_beneath_the_rpc(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nothing below the interceptor is passed the ID; it reads it from here."""
    with tracking_call("a-correlation-id"):
        record = a_record()

    with tracking_call("a-correlation-id"):
        assert render(record)["correlation_id"] == "a-correlation-id"

    assert "correlation_id" not in render(a_record())


def test_an_upstream_status_recorded_mid_call_is_visible_to_the_interceptor() -> None:
    """The Bridge's HTTP status belongs on the RPC's own log line, and the
    layer that knows it is several frames below the one that writes it."""
    with tracking_call(new_correlation_id()) as call:
        record_upstream_status(207)

        assert call.upstream_status == 207

    assert current_call() is None


def test_recording_an_upstream_status_outside_a_call_is_not_an_error() -> None:
    """A Bridge call made at startup has no RPC to belong to."""
    record_upstream_status(200)


def test_correlation_ids_are_unique_and_url_safe() -> None:
    ids = {new_correlation_id() for _ in range(100)}

    assert len(ids) == 100
    assert all(identifier.isalnum() for identifier in ids)


def test_configuring_logging_twice_does_not_log_everything_twice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """systemd restarts the process; a test suite does not."""
    try:
        configure_logging(level="INFO", style="json")
        configure_logging(level="INFO", style="json")

        logging.getLogger("hue_grpc.test").info("once")

        assert capsys.readouterr().err.count('"message": "once"') == 1
    finally:
        logging.getLogger().handlers.clear()


def test_the_text_style_stays_readable_for_a_person(
    capsys: pytest.CaptureFixture[str],
) -> None:
    try:
        configure_logging(level="INFO", style="text")

        logging.getLogger("hue_grpc.test").info(
            "rpc finished", **fields(rpc="/hue.v1.Test/Echo", status="OK")
        )

        printed = capsys.readouterr().err
        assert "rpc finished" in printed
        assert "rpc=/hue.v1.Test/Echo" in printed
        assert "status=OK" in printed
    finally:
        logging.getLogger().handlers.clear()
