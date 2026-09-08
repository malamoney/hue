"""A fake Philips Hue Bridge: just enough CLIP v2 to drive the Gateway.

It speaks the three request shapes the Gateway makes of a real Bridge — read
the light collection, change one light, hold the event stream open — over TLS,
with a certificate the Gateway verifies for real (see ``certs``). Everything
answers in the CLIP v2 envelope: a ``data`` array and an ``errors`` array,
both always present.

What it is *not* is a simulator of a real Bridge's behaviour beyond that. It
keeps light state in memory, applies only the command fields the Gateway
sends (``on`` and ``dimming``), and emits one event per accepted change. It
also serves the v1 ``POST /api`` pairing exchange — always minting, never
waiting on a link button — so a test can register a Bridge the way a real
deployment that is not statically configured does.

``disconnect_streams`` drops every open event-stream connection, which is how
a test reproduces a Bridge that fell off the network without unplugging
anything.
"""

from __future__ import annotations

import json
import queue
import secrets
import socketserver
import ssl
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

from fake_hue.certs import BridgeCerts

__all__ = [
    "APPLICATION_KEY_HEADER",
    "DEFAULT_LIGHTS",
    "PAIRING_PATH",
    "FakeHueBridge",
    "LightStore",
]

APPLICATION_KEY_HEADER = "hue-application-key"

#: The v1 endpoint pairing has always lived on. Unauthenticated: pairing is
#: what mints the key, so it cannot require one.
PAIRING_PATH = "/api"

_LIGHT_COLLECTION = "/clip/v2/resource/light"
_EVENT_STREAM = "/eventstream/clip/v2"

#: A queue entry that tells a streaming handler to close rather than deliver.
_CLOSE = object()


def _light(
    light_id: str, name: str, *, on: bool = True, brightness: float = 80.0
) -> dict[str, Any]:
    """One light resource, in the shape CLIP v2 reports."""
    return {
        "id": light_id,
        "id_v1": f"/lights/{name}",
        "type": "light",
        "owner": {
            "rid": str(uuid.uuid5(uuid.NAMESPACE_DNS, light_id)),
            "rtype": "device",
        },
        "metadata": {"name": name, "archetype": "sultan_bulb"},
        "on": {"on": on},
        "dimming": {"brightness": brightness, "min_dim_level": 0.2},
        "mode": "normal",
    }


#: The light collection a freshly started fake serves. Two lights, so a test
#: can tell "changed the one I asked for" from "changed something".
DEFAULT_LIGHTS: tuple[dict[str, Any], ...] = (
    _light("bbbbbbbb-0000-4000-8000-000000000001", "Desk"),
    _light("bbbbbbbb-0000-4000-8000-000000000002", "Shelf", on=False, brightness=40.0),
)


class LightStore:
    """In-memory light state, and the fan-out to open event streams.

    One lock guards both the resources and the subscriber list: a change
    mutates a resource and notifies subscribers as one step, so a stream that
    connected mid-change sees either all of it or none.
    """

    def __init__(self, lights: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        if lights is None:
            lights = {light["id"]: light for light in DEFAULT_LIGHTS}
        self._lights: dict[str, dict[str, Any]] = {
            light_id: _copy(resource) for light_id, resource in lights.items()
        }
        self._subscribers: list[queue.Queue[Any]] = []
        self._lock = threading.Lock()

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_copy(light) for light in self._lights.values()]

    def one(self, light_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._lights.get(light_id)
            return None if found is None else _copy(found)

    def subscribe(self) -> queue.Queue[Any]:
        channel: queue.Queue[Any] = queue.Queue()
        with self._lock:
            self._subscribers.append(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue[Any]) -> None:
        with self._lock:
            if channel in self._subscribers:
                self._subscribers.remove(channel)

    def disconnect_all(self) -> None:
        """Tell every open stream to close: a Bridge dropping off the network."""
        with self._lock:
            for channel in self._subscribers:
                channel.put(_CLOSE)
            self._subscribers.clear()

    def apply(self, light_id: str, command: Mapping[str, Any]) -> dict[str, Any] | None:
        """Merge ``command`` into one light and notify streams. ``None`` if unknown.

        Only ``on`` and ``dimming`` are honoured — the only groups the Gateway
        ever sends — and each is merged, not replaced, so a brightness change
        leaves the power alone exactly as it would on a real Bridge.
        """
        with self._lock:
            light = self._lights.get(light_id)
            if light is None:
                return None
            for group in ("on", "dimming"):
                if group in command and isinstance(command[group], Mapping):
                    light.setdefault(group, {}).update(command[group])
            event = _change_event(light)
            for channel in self._subscribers:
                channel.put(event)
            return _copy(light)


def _change_event(light: Mapping[str, Any]) -> dict[str, Any]:
    """A CLIP v2 ``update`` event naming one changed light."""
    return {
        "creationtime": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": [_copy(light)],
        "id": str(uuid.uuid4()),
        "type": "update",
    }


def _envelope(data: list[Any], errors: list[Any] | None = None) -> bytes:
    return json.dumps({"errors": errors or [], "data": data}).encode()


def _copy(resource: Mapping[str, Any]) -> dict[str, Any]:
    """A deep copy, so a caller cannot reach back into stored light state."""
    return cast("dict[str, Any]", json.loads(json.dumps(resource)))


def _host_port(address: Any) -> tuple[str, int]:
    host, port = address[:2]
    return str(host), int(port)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # http.server's own server_bind runs a reverse DNS lookup on the bind
        # address to fill server_name. Nothing here uses it, and the lookup
        # stalls on a host with slow or no DNS — every VM node, to start with.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = _host_port(self.server_address)


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeHue/0"
    protocol_version = "HTTP/1.1"

    @property
    def _bridge(self) -> FakeHueBridge:
        bridge: FakeHueBridge = self.server.bridge  # type: ignore[attr-defined]
        return bridge

    def log_message(self, format: str, *args: Any) -> None:
        if self._bridge.log:
            super().log_message(format, *args)

    # BaseHTTPRequestHandler dispatches on do_<VERB>.
    def do_GET(self) -> None:
        if self.path == _LIGHT_COLLECTION:
            if self._require_key():
                self._json(_envelope(self._bridge.lights.all()))
        elif self.path.startswith(f"{_LIGHT_COLLECTION}/"):
            self._get_one(self.path.removeprefix(f"{_LIGHT_COLLECTION}/"))
        elif self.path == _EVENT_STREAM:
            self._event_stream()
        else:
            self._not_found()

    def do_PUT(self) -> None:
        if self.path.startswith(f"{_LIGHT_COLLECTION}/"):
            self._put_one(self.path.removeprefix(f"{_LIGHT_COLLECTION}/"))
        else:
            self._not_found()

    def do_POST(self) -> None:
        if self.path == PAIRING_PATH:
            self._pair()
        else:
            self._not_found()

    def _pair(self) -> None:
        """The v1 pairing exchange: mint both keys and hand them back.

        Unauthenticated, and the link button is always "pressed" — the
        unpressed-button path (Hue error type 101) is a unit-test concern in
        the Gateway, not something a booted VM needs to pass through.
        """
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        application_key, client_key = self._bridge.mint_pairing()
        self._json(
            json.dumps(
                [{"success": {"username": application_key, "clientkey": client_key}}]
            ).encode()
        )

    def _get_one(self, light_id: str) -> None:
        if not self._require_key():
            return
        light = self._bridge.lights.one(light_id)
        if light is None:
            self._json(
                _envelope([], [{"description": f"resource {light_id} not found"}]),
                status=HTTPStatus.NOT_FOUND,
            )
            return
        self._json(_envelope([light]))

    def _put_one(self, light_id: str) -> None:
        if not self._require_key():
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            command = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._json(
                _envelope([], [{"description": "body is not JSON"}]),
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        updated = self._bridge.lights.apply(light_id, command)
        if updated is None:
            self._json(
                _envelope([], [{"description": f"resource {light_id} not found"}]),
                status=HTTPStatus.NOT_FOUND,
            )
            return
        self._json(_envelope([{"rid": light_id, "rtype": "light"}]))

    def _event_stream(self) -> None:
        if not self._require_key():
            return
        channel = self._bridge.lights.subscribe()
        # The body has no length and is delimited by the connection closing,
        # so the connection must not be reused: without this the handler
        # returns into keep-alive and the client never sees the stream end.
        self.close_connection = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        counter = 0
        try:
            self.wfile.write(b": hi\n\n")
            self.wfile.flush()
            while not self._bridge.stopping.is_set():
                try:
                    item = channel.get(timeout=1.0)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                if item is _CLOSE:
                    return
                counter += 1
                frame = f"id: {counter}\ndata: {json.dumps([item])}\n\n"
                self.wfile.write(frame.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            self._bridge.lights.unsubscribe(channel)

    def _require_key(self) -> bool:
        """True when the request carried the expected Application Key.

        A real Bridge answers 403 with an error envelope for a missing or
        wrong key, and the Gateway's transport turns that into a failure. The
        check is here so a test can prove the header is being sent — and, with
        the journal grep, that it is not being logged.
        """
        presented = self.headers.get(APPLICATION_KEY_HEADER)
        if presented and self._bridge.accepts_key(presented):
            return True
        self._json(
            _envelope([], [{"description": "unauthorized user"}]),
            status=HTTPStatus.FORBIDDEN,
        )
        return False

    def _not_found(self) -> None:
        self._json(
            _envelope([], [{"description": f"no such path {self.path}"}]),
            status=HTTPStatus.NOT_FOUND,
        )

    def _json(self, body: bytes, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeHueBridge:
    """A running fake Bridge. Use as a context manager."""

    def __init__(
        self,
        certs: BridgeCerts,
        *,
        application_key: str,
        host: str = "127.0.0.1",
        port: int = 0,
        lights: Mapping[str, Mapping[str, Any]] | None = None,
        log: bool = False,
    ) -> None:
        #: The key handed over out of band — the static-config path, where the
        #: Gateway is given its key rather than pairing for one.
        self.application_key = application_key
        #: Keys this fake has minted through pairing, kept so the CLIP calls
        #: that follow are accepted. Not durable: a restart forgets them, the
        #: same way a real Bridge would not.
        self._minted: set[str] = set()
        self.lights = LightStore(lights)
        self.log = log
        self.stopping = threading.Event()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certs.cert_file, certs.key_file)
        self._server = _Server((host, port), _Handler)
        self._server.bridge = self  # type: ignore[attr-defined]
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def address(self) -> str:
        host, port = _host_port(self._server.server_address)
        return f"{host}:{port}"

    def accepts_key(self, key: str) -> bool:
        return key == self.application_key or key in self._minted

    def mint_pairing(self) -> tuple[str, str]:
        """A fresh Application Key and Client Key, both now accepted."""
        application_key = secrets.token_urlsafe(24)
        client_key = secrets.token_hex(16).upper()
        self._minted.add(application_key)
        return application_key, client_key

    def disconnect_streams(self) -> None:
        self.lights.disconnect_all()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.lights.disconnect_all()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)

    def __enter__(self) -> FakeHueBridge:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
