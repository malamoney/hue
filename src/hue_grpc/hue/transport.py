"""The HTTP transport that talks to one Bridge.

This is the only layer that knows the Application Key exists: it applies the
`hue-application-key` header and keeps the header out of logs. It speaks both
CLIP v2 and the v1 `POST /api` endpoint, because Pairing lives on the old API.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from hue_grpc.hue.tls import bridge_ssl_context

__all__ = [
    "APPLICATION_KEY_HEADER",
    "DEFAULT_TIMEOUTS",
    "BridgeResponseError",
    "BridgeTimeoutError",
    "BridgeUnreachableError",
    "HueTransport",
    "HueTransportError",
    "MalformedResponseError",
    "Timeouts",
    "redact_headers",
]

APPLICATION_KEY_HEADER = "hue-application-key"

_log = logging.getLogger(__name__)

_REDACTED = "<redacted>"


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Headers with the Application Key removed, safe to log or trace."""
    return {
        name: _REDACTED if name.lower() == APPLICATION_KEY_HEADER else value
        for name, value in headers.items()
    }


class HueTransportError(Exception):
    """The Bridge could not be reached, or answered with something unusable."""


class BridgeResponseError(HueTransportError):
    """The Bridge answered with a non-success status.

    Hue reports application-level failures inside a 200 response, so this is a
    transport-level failure: an unusable Application Key, a path the Bridge
    does not serve, or the Bridge itself failing.
    """

    def __init__(self, status_code: int, payload: Any) -> None:
        super().__init__(f"bridge returned HTTP {status_code}")
        self.status_code = status_code
        self.payload = payload


class MalformedResponseError(HueTransportError):
    """The Bridge answered with a body that is not the JSON we expect."""


class BridgeUnreachableError(HueTransportError):
    """The connection to the Bridge could not be made or was lost."""


class BridgeTimeoutError(HueTransportError):
    """The Bridge did not answer within the configured timeout."""


@dataclass(frozen=True)
class Timeouts:
    """Bounds on a Bridge call, kept distinct rather than collapsed into one.

    Reaching a Bridge that has moved or gone away fails fast; a Bridge that
    has accepted the connection is given longer to answer. The event stream
    is silent between events, so it is not bounded by `read` at all.
    """

    connect: float = 5.0
    read: float = 10.0
    write: float = 10.0
    pool: float = 5.0
    stream_read: float | None = None

    def httpx_timeout(self, *, read: float | None) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect, read=read, write=self.write, pool=self.pool
        )


DEFAULT_TIMEOUTS = Timeouts()


class HueTransport:
    """An HTTPS connection pool for one Bridge, verified as that Bridge."""

    def __init__(
        self,
        *,
        bridge_id: str,
        address: str,
        application_key: str | None = None,
        timeouts: Timeouts = DEFAULT_TIMEOUTS,
        ca_pem: str | None = None,
    ) -> None:
        #: Set once Pairing mints one; until then requests go out unauthenticated.
        self.application_key = application_key
        self._timeouts = timeouts
        self._client = httpx.AsyncClient(
            base_url=f"https://{address}",
            verify=bridge_ssl_context(bridge_id, ca_pem=ca_pem),
            timeout=timeouts.httpx_timeout(read=timeouts.read),
        )

    async def __aenter__(self) -> HueTransport:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, *, json: Any = None) -> Any:
        """Make one request to the Bridge and return its decoded JSON body."""
        request = self._client.build_request(
            method, path, json=json, headers=self._headers()
        )
        response = await self._send(request)
        if not response.is_success:
            raise BridgeResponseError(response.status_code, _failure_payload(response))
        return _decode(response)

    @asynccontextmanager
    async def stream(
        self, method: str, path: str, *, headers: Mapping[str, str] | None = None
    ) -> AsyncIterator[httpx.Response]:
        """Hold open a streaming response, such as the Bridge's event stream.

        The body is left unread: the caller consumes it as it arrives. Only
        `stream_read` bounds the wait between chunks, so a quiet Bridge is not
        mistaken for a wedged one.
        """
        request = self._client.build_request(
            method,
            path,
            headers={**self._headers(), **(headers or {})},
            timeout=self._timeouts.httpx_timeout(read=self._timeouts.stream_read),
        )
        response = await self._send(request, stream=True)
        try:
            if not response.is_success:
                await response.aread()
                raise BridgeResponseError(
                    response.status_code, _failure_payload(response)
                )
            yield response
        finally:
            await response.aclose()

    async def _send(
        self, request: httpx.Request, *, stream: bool = False
    ) -> httpx.Response:
        """Send a prepared request, reporting failures in this layer's terms."""
        _log.debug(
            "bridge request %s %s headers=%s",
            request.method,
            request.url,
            redact_headers(request.headers),
        )
        try:
            response = await self._client.send(request, stream=stream)
        except httpx.TimeoutException as timeout:
            raise BridgeTimeoutError(str(timeout) or repr(timeout)) from timeout
        except httpx.TransportError as unreachable:
            raise BridgeUnreachableError(
                str(unreachable) or repr(unreachable)
            ) from unreachable
        _log.debug(
            "bridge response %s %s -> %s",
            request.method,
            request.url,
            response.status_code,
        )
        return response

    def _headers(self) -> dict[str, str]:
        if self.application_key is None:
            return {}
        return {APPLICATION_KEY_HEADER: self.application_key}


def _decode(response: httpx.Response) -> Any:
    """Decode a Bridge response body, or say so when it is not JSON.

    An empty body decodes to `None`: some Bridge calls answer with no content,
    and that is an answer rather than a malformed one.
    """
    if not response.content:
        return None
    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as undecodable:
        raise MalformedResponseError(
            f"bridge returned a {response.headers.get('content-type', 'typeless')} "
            f"body with HTTP {response.status_code}"
        ) from undecodable


def _failure_payload(response: httpx.Response) -> Any:
    """Whatever a failed response carried: its error envelope, or its raw text.

    A Bridge that fails outside the API — a 503 served as HTML, say — must
    still reach the caller as a failure with its status, not as an unrelated
    decoding problem.
    """
    try:
        return _decode(response)
    except MalformedResponseError:
        return response.text
