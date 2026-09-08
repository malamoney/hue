"""The fake Bridge answers the CLIP v2 subset the Gateway speaks."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, cast

import httpx
from fake_hue.bridge import APPLICATION_KEY_HEADER, FakeHueBridge

_LIGHTS = "/clip/v2/resource/light"
_STREAM = "/eventstream/clip/v2"


def test_lists_lights_in_the_clip_envelope(client: httpx.Client) -> None:
    response = client.get(_LIGHTS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["errors"] == []
    assert {light["type"] for light in payload["data"]} == {"light"}
    assert len(payload["data"]) == 2


def test_a_missing_application_key_is_refused(client: httpx.Client) -> None:
    response = client.get(_LIGHTS, headers={APPLICATION_KEY_HEADER: ""})

    assert response.status_code == 403
    assert response.json()["errors"]


def test_an_unknown_light_is_a_404_with_an_error_envelope(
    client: httpx.Client,
) -> None:
    response = client.get(f"{_LIGHTS}/nope")

    assert response.status_code == 404
    assert response.json()["errors"]


def test_a_change_merges_and_leaves_the_rest_alone(client: httpx.Client) -> None:
    listed = client.get(_LIGHTS).json()["data"]
    on_light = next(light for light in listed if light["on"]["on"])
    brightness = on_light["dimming"]["brightness"]

    changed = client.put(f"{_LIGHTS}/{on_light['id']}", json={"on": {"on": False}})

    assert changed.status_code == 200
    assert changed.json()["data"] == [{"rid": on_light["id"], "rtype": "light"}]

    after = client.get(f"{_LIGHTS}/{on_light['id']}").json()["data"][0]
    assert after["on"]["on"] is False
    assert after["dimming"]["brightness"] == brightness


def test_changing_an_unknown_light_is_a_404(client: httpx.Client) -> None:
    assert client.put(f"{_LIGHTS}/nope", json={"on": {"on": True}}).status_code == 404


def _first_frame(
    response: httpx.Response, *, deadline: float = 5.0
) -> list[dict[str, Any]]:
    """The payload of the first real server-sent event on ``response``."""
    payload: list[str] = []
    ends_at = time.monotonic() + deadline
    for line in response.iter_lines():
        if time.monotonic() > ends_at:
            raise AssertionError("no event frame arrived")
        if line.startswith(":"):
            continue
        if line == "":
            if payload:
                return cast("list[dict[str, Any]]", json.loads("\n".join(payload)))
            continue
        name, _, value = line.partition(":")
        if name == "data":
            payload.append(value.removeprefix(" "))
    raise AssertionError("the stream ended before a frame arrived")


def test_the_event_stream_carries_a_frame_per_change(client: httpx.Client) -> None:
    with client.stream("GET", _STREAM) as stream:
        assert stream.status_code == 200
        listed = client.get(_LIGHTS).json()["data"]
        target = listed[0]

        def _change() -> None:
            time.sleep(0.2)
            client.put(
                f"{_LIGHTS}/{target['id']}", json={"dimming": {"brightness": 12.0}}
            )

        changer = threading.Thread(target=_change)
        changer.start()
        try:
            frame = _first_frame(stream)
        finally:
            changer.join()

    assert frame[0]["type"] == "update"
    assert frame[0]["data"][0]["id"] == target["id"]
    assert frame[0]["data"][0]["dimming"]["brightness"] == 12.0


def test_disconnect_streams_ends_every_open_stream(
    bridge: FakeHueBridge, client: httpx.Client
) -> None:
    with client.stream("GET", _STREAM) as stream:
        assert stream.status_code == 200

        def _drop() -> None:
            time.sleep(0.2)
            bridge.disconnect_streams()

        dropper = threading.Thread(target=_drop)
        dropper.start()
        try:
            # Reading to exhaustion returns rather than blocking once the
            # server closes its end.
            lines = list(stream.iter_lines())
        finally:
            dropper.join()

    assert not any(line.startswith("data:") for line in lines)


def test_control_disconnect_is_also_reachable_over_http(client: httpx.Client) -> None:
    assert client.post("/__control__/disconnect-streams").status_code == 200
