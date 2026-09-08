"""The cross-cutting chain every RPC passes through.

Wired in from the first day the server exists, before there is a single
service to intercept. Retrofitting a chain means touching every RPC path that
grew up without one, and the no-op case — no Gateway Token configured — costs
one comparison against `None` per call.

The chain runs outside-in, and the order is the point:

1. `ObservabilityInterceptor` — outermost, so that a call rejected by
   authentication, which never reaches a handler, still leaves a log line.
2. `AuthInterceptor` — nothing runs a handler before the caller is known.
3. `DeadlineInterceptor` — innermost, so the clock covers the handler and
   nothing else.

Every interceptor here works by replacing a handler's behaviour and keeping
its cardinality, rather than substituting a handler of its own: a unary
handler standing in for a streaming one is accepted at registration time and
fails when a client finally calls it.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any

import grpc

from hue_grpc.logs import Call, fields, new_correlation_id, tracking_call

__all__ = [
    "CORRELATION_ID_METADATA",
    "AuthInterceptor",
    "DeadlineInterceptor",
    "ObservabilityInterceptor",
]

#: What a client sends to name its own correlation ID, so that a trace which
#: started somewhere else stays one trace. gRPC metadata keys are lowercase
#: ASCII; anything else the library rejects before we see it.
CORRELATION_ID_METADATA = "x-correlation-id"

_AUTHORIZATION = "authorization"
_BEARER = "bearer"

_UNAUTHENTICATED = "missing or invalid gateway token"

_log = logging.getLogger(__name__)


def _service_of(method: str) -> str:
    """`hue.v1.Lighting` out of `/hue.v1.Lighting/GetLight`."""
    return method.split("/")[1] if method.startswith("/") else method


def _metadata_value(metadata: Iterable[tuple[str, Any]] | None, key: str) -> str | None:
    for name, value in metadata or ():
        if name.lower() == key and isinstance(value, str):
            return value
    return None


def _behaviour(handler: grpc.RpcMethodHandler) -> Callable[..., Any]:
    """The one callable a handler actually carries."""
    behaviour = (
        handler.unary_unary
        or handler.unary_stream
        or handler.stream_unary
        or handler.stream_stream
    )
    if behaviour is None:
        raise ValueError(f"rpc handler for a registered method carries none: {handler}")
    return behaviour  # type: ignore[no-any-return]


def _replacing(
    handler: grpc.RpcMethodHandler, behaviour: Callable[..., Any]
) -> grpc.RpcMethodHandler:
    """`handler` with new behaviour and the cardinality it was registered with."""
    if handler.request_streaming and handler.response_streaming:
        build = grpc.stream_stream_rpc_method_handler
    elif handler.request_streaming:
        build = grpc.stream_unary_rpc_method_handler
    elif handler.response_streaming:
        build = grpc.unary_stream_rpc_method_handler
    else:
        build = grpc.unary_unary_rpc_method_handler
    replaced = build(
        behaviour,
        request_deserializer=handler.request_deserializer,
        response_serializer=handler.response_serializer,
    )
    return replaced


class ObservabilityInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
    """One structured log line per RPC, and a correlation ID for the rest.

    The line carries the method, the gRPC status the client saw, how long it
    took, the peer, and the upstream HTTP status if any layer below recorded
    one. It carries no metadata: both the Gateway Token and — were anything
    ever to put one there — the Application Key travel in metadata, so no
    metadata is ever a field.

    A client may name its own correlation ID with `x-correlation-id`, which is
    how a trace started elsewhere stays one trace. Otherwise one is minted.
    """

    async def intercept_service(
        self,
        continuation: Callable[[Any], Awaitable[grpc.RpcMethodHandler | None]],
        handler_call_details: Any,
    ) -> grpc.RpcMethodHandler | None:
        method = handler_call_details.method
        correlation_id = (
            _metadata_value(
                handler_call_details.invocation_metadata, CORRELATION_ID_METADATA
            )
            or new_correlation_id()
        )
        handler = await continuation(handler_call_details)
        if handler is None:
            # gRPC answers UNIMPLEMENTED itself, without ever calling a
            # handler, so this is the only chance to account for the call.
            _log.warning(
                "rpc unimplemented",
                **fields(
                    correlation_id=correlation_id,
                    rpc=method,
                    status=grpc.StatusCode.UNIMPLEMENTED.name,
                ),
            )
            return None
        behaviour = _behaviour(handler)

        if handler.response_streaming:

            async def observed_stream(request: Any, context: Any) -> AsyncIterator[Any]:
                with tracking_call(correlation_id) as call:
                    started = time.perf_counter()
                    try:
                        produced = behaviour(request, context)
                        if hasattr(produced, "__aiter__"):
                            async for response in produced:
                                yield response
                        else:
                            # A handler that writes through the context rather
                            # than yielding. Both shapes are legal.
                            await produced
                    except BaseException as failure:
                        _finish(method, context, call, started, failure)
                        raise
                    _finish(method, context, call, started, None)

            return _replacing(handler, observed_stream)

        async def observed(request: Any, context: Any) -> Any:
            with tracking_call(correlation_id) as call:
                started = time.perf_counter()
                try:
                    response = await behaviour(request, context)
                except BaseException as failure:
                    _finish(method, context, call, started, failure)
                    raise
                _finish(method, context, call, started, None)
                return response

        return _replacing(handler, observed)


def _finish(
    method: str,
    context: Any,
    call: Call,
    started: float,
    failure: BaseException | None,
) -> None:
    """Write the one line this RPC leaves behind."""
    code = _status_of(context, failure)
    unexpected = failure is not None and code is grpc.StatusCode.UNKNOWN
    level = (
        logging.ERROR
        if unexpected
        else logging.INFO
        if code is grpc.StatusCode.OK
        else logging.WARNING
    )
    attached = {
        "correlation_id": call.correlation_id,
        "rpc": method,
        "status": code.name,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "peer": context.peer(),
        "upstream_status": call.upstream_status,
    }
    _log.log(
        level,
        "rpc finished",
        exc_info=failure if unexpected else None,
        **fields(**{k: v for k, v in attached.items() if v is not None}),
    )


def _status_of(context: Any, failure: BaseException | None) -> grpc.StatusCode:
    """The status the client saw, which is not always what was raised."""
    code = context.code()
    if isinstance(code, grpc.StatusCode):
        return code
    if failure is None:
        return grpc.StatusCode.OK
    if isinstance(failure, asyncio.CancelledError | GeneratorExit):
        # The client hung up, or the deadline passed and gRPC cancelled us.
        # A closed response stream arrives as GeneratorExit rather than as a
        # cancellation, and a hang-up is not an error anyone needs paging for.
        return grpc.StatusCode.CANCELLED
    return grpc.StatusCode.UNKNOWN


class AuthInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
    """A bearer-token check, or nothing at all when no token is configured.

    Nothing at all is the default, because the default listener is loopback
    where the only callers are already on the host. Setting a token is a
    configuration change, and this interceptor is in the chain either way so
    that making it is not a change to any RPC path.

    `exempt_services` is for the health service: a supervisor asking whether
    the process is up should not be told that its token is wrong, which is an
    answer to a different question.
    """

    def __init__(
        self, token: str | None, *, exempt_services: frozenset[str] = frozenset()
    ) -> None:
        self._token = token
        self._exempt = exempt_services

    async def intercept_service(
        self,
        continuation: Callable[[Any], Awaitable[grpc.RpcMethodHandler | None]],
        handler_call_details: Any,
    ) -> grpc.RpcMethodHandler | None:
        handler = await continuation(handler_call_details)
        if handler is None or self._allowed(handler_call_details):
            return handler

        if handler.response_streaming:

            async def denied_stream(request: Any, context: Any) -> AsyncIterator[Any]:
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, _UNAUTHENTICATED)
                yield  # pragma: no cover - abort does not return

            return _replacing(handler, denied_stream)

        async def denied(request: Any, context: Any) -> Any:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, _UNAUTHENTICATED)

        return _replacing(handler, denied)

    def _allowed(self, handler_call_details: Any) -> bool:
        if self._token is None:
            return True
        if _service_of(handler_call_details.method) in self._exempt:
            return True
        presented = _metadata_value(
            handler_call_details.invocation_metadata, _AUTHORIZATION
        )
        if presented is None:
            return False
        scheme, _, token = presented.partition(" ")
        if scheme.lower() != _BEARER:
            return False
        # Constant-time: a token compared byte by byte can be guessed one byte
        # at a time by a caller that can time the answer.
        return hmac.compare_digest(token, self._token)


class DeadlineInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
    """A default deadline for unary calls that arrived without one.

    A client that forgets a deadline would otherwise be able to hold an
    upstream Bridge request open indefinitely. A client that set its own keeps
    it, longer or shorter: Pairing waits on somebody walking to the Bridge,
    and that is the client's call to make rather than this one's.

    Response-streaming RPCs are left alone. The event stream is silent between
    events and is meant to outlive any deadline; bounding it here would close
    it on a schedule.

    `asyncio.wait_for` cancels the handler's task when the deadline passes,
    and that cancellation is what unwinds the in-flight Bridge request
    underneath it.
    """

    def __init__(self, default_deadline: float) -> None:
        self._deadline = default_deadline

    async def intercept_service(
        self,
        continuation: Callable[[Any], Awaitable[grpc.RpcMethodHandler | None]],
        handler_call_details: Any,
    ) -> grpc.RpcMethodHandler | None:
        handler = await continuation(handler_call_details)
        if handler is None or handler.response_streaming:
            return handler
        behaviour = _behaviour(handler)
        deadline = self._deadline

        async def bounded(request: Any, context: Any) -> Any:
            if context.time_remaining() is not None:
                return await behaviour(request, context)
            try:
                return await asyncio.wait_for(behaviour(request, context), deadline)
            except TimeoutError:
                await context.abort(
                    grpc.StatusCode.DEADLINE_EXCEEDED,
                    f"no deadline was set and the gateway's default of "
                    f"{deadline:g}s passed",
                )

        return _replacing(handler, bounded)
