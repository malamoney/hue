"""The interceptor chain, exercised through a real `grpc.aio` server.

Every test here goes over a real socket with a real client, because an
interceptor that behaves correctly when called directly and wrongly when gRPC
calls it is the failure worth catching. The service is a stand-in: no
generated service exists yet, and an interceptor never sees one anyway.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import grpc
import pytest
from conftest import (
    BOOM,
    DENIED,
    ECHO,
    ECHO_STREAM,
    SLEEP,
    SLEEP_STREAM,
    UNKNOWN,
    UPSTREAM,
    echo_server,
    run,
)

from hue_grpc.serving.interceptors import (
    CORRELATION_ID_METADATA,
    AuthInterceptor,
    DeadlineInterceptor,
    ObservabilityInterceptor,
)

TOKEN = "a-gateway-token"


async def call(
    target: str,
    method: str,
    payload: bytes = b"hello",
    *,
    metadata: Any = None,
    timeout: float | None = None,
) -> bytes:
    async with grpc.aio.insecure_channel(target) as channel:
        response = await channel.unary_unary(method)(
            payload, metadata=metadata, timeout=timeout
        )
        assert isinstance(response, bytes)
        return response


async def stream(
    target: str,
    method: str,
    payload: bytes = b"hello",
    *,
    metadata: Any = None,
    timeout: float | None = None,
) -> list[bytes]:
    async with grpc.aio.insecure_channel(target) as channel:
        stream_call = channel.unary_stream(method)(
            payload, metadata=metadata, timeout=timeout
        )
        return [response async for response in stream_call]


def status_of(failure: grpc.aio.AioRpcError) -> grpc.StatusCode:
    return failure.code()


# --- authentication ---------------------------------------------------------


def test_no_token_configured_leaves_the_interceptor_in_the_chain_doing_nothing() -> (
    None
):
    """The chain is wired from day one so that turning auth on later is a
    config change, not a change to every RPC path."""

    async def scenario() -> None:
        async with echo_server(AuthInterceptor(None)) as target:
            assert await call(target, ECHO) == b"hello"

    run(scenario())


def test_a_configured_token_is_required() -> None:
    async def scenario() -> None:
        async with echo_server(AuthInterceptor(TOKEN)) as target:
            with pytest.raises(grpc.aio.AioRpcError) as failure:
                await call(target, ECHO)

            assert status_of(failure.value) == grpc.StatusCode.UNAUTHENTICATED

    run(scenario())


def test_the_right_bearer_token_gets_through() -> None:
    async def scenario() -> None:
        async with echo_server(AuthInterceptor(TOKEN)) as target:
            metadata = (("authorization", f"Bearer {TOKEN}"),)

            assert await call(target, ECHO, metadata=metadata) == b"hello"

    run(scenario())


def test_a_wrong_token_or_a_wrong_scheme_does_not() -> None:
    async def scenario() -> None:
        async with echo_server(AuthInterceptor(TOKEN)) as target:
            for header in (
                f"Bearer not-{TOKEN}",
                f"Basic {TOKEN}",
                TOKEN,
                "Bearer ",
            ):
                with pytest.raises(grpc.aio.AioRpcError) as failure:
                    await call(target, ECHO, metadata=(("authorization", header),))

                assert status_of(failure.value) == grpc.StatusCode.UNAUTHENTICATED

    run(scenario())


def test_a_streaming_method_is_guarded_too() -> None:
    """A wrapper that only understood unary handlers would wave this through."""

    async def scenario() -> None:
        async with echo_server(AuthInterceptor(TOKEN)) as target:
            with pytest.raises(grpc.aio.AioRpcError) as failure:
                await stream(target, ECHO_STREAM)

            assert status_of(failure.value) == grpc.StatusCode.UNAUTHENTICATED

            metadata = (("authorization", f"Bearer {TOKEN}"),)
            assert (
                await stream(target, ECHO_STREAM, metadata=metadata) == [b"hello"] * 3
            )

    run(scenario())


def test_an_exempt_service_is_reachable_without_a_token() -> None:
    """Health is how a supervisor asks whether the process is up; answering
    'your token is wrong' to that question reports the wrong thing."""

    async def scenario() -> None:
        exempting = AuthInterceptor(TOKEN, exempt_services=frozenset({"hue.v1.Echo"}))
        async with echo_server(exempting) as target:
            assert await call(target, ECHO) == b"hello"

    run(scenario())


# --- deadlines --------------------------------------------------------------


def test_a_call_without_a_deadline_gets_the_default_one() -> None:
    async def scenario() -> None:
        async with echo_server(DeadlineInterceptor(0.05)) as target:
            with pytest.raises(grpc.aio.AioRpcError) as failure:
                await call(target, SLEEP, b"5")

            assert status_of(failure.value) == grpc.StatusCode.DEADLINE_EXCEEDED

    run(scenario())


def test_a_client_that_set_its_own_deadline_keeps_it() -> None:
    """The default is a floor under forgetful clients, not a ceiling over
    deliberate ones — pairing waits on a person walking to the Bridge."""

    async def scenario() -> None:
        async with echo_server(DeadlineInterceptor(0.05)) as target:
            assert await call(target, SLEEP, b"0.3", timeout=5) == b"0.3"

    run(scenario())


def test_a_response_stream_is_not_bounded_by_the_unary_default() -> None:
    """The event stream is silent between events and outlives any deadline."""

    async def scenario() -> None:
        async with echo_server(DeadlineInterceptor(0.05)) as target:
            assert await stream(target, SLEEP_STREAM, b"0.2") == [b"0.2"]

    run(scenario())


# --- observability ----------------------------------------------------------


def records(caplog: pytest.LogCaptureFixture) -> list[Any]:
    """The interceptor's own records. `Any`, because the fields under test are
    attached to the record rather than declared on it."""
    return [record for record in caplog.records if record.name.endswith("interceptors")]


def test_one_record_per_rpc_names_the_method_status_and_duration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        async with echo_server(ObservabilityInterceptor()) as target:
            await call(target, ECHO)

    with caplog.at_level(logging.INFO):
        run(scenario())

    (record,) = records(caplog)
    assert record.rpc == ECHO
    assert record.status == "OK"
    assert record.duration_ms >= 0
    assert record.correlation_id
    assert record.peer.startswith("ipv4:")


def test_a_client_supplied_correlation_id_is_the_one_that_is_used(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        async with echo_server(ObservabilityInterceptor()) as target:
            await call(
                target, ECHO, metadata=((CORRELATION_ID_METADATA, "from-the-client"),)
            )

    with caplog.at_level(logging.INFO):
        run(scenario())

    (record,) = records(caplog)
    assert record.correlation_id == "from-the-client"


def test_each_rpc_without_one_gets_its_own(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        async with echo_server(ObservabilityInterceptor()) as target:
            await call(target, ECHO)
            await call(target, ECHO)

    with caplog.at_level(logging.INFO):
        run(scenario())

    first, second = records(caplog)
    assert first.correlation_id != second.correlation_id


def test_the_upstream_status_the_handler_saw_lands_on_the_rpcs_own_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        async with echo_server(ObservabilityInterceptor()) as target:
            await call(target, UPSTREAM, b"207")

    with caplog.at_level(logging.INFO):
        run(scenario())

    (record,) = records(caplog)
    assert record.upstream_status == 207


def test_an_aborted_rpc_is_logged_with_the_status_the_client_saw(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        async with echo_server(ObservabilityInterceptor()) as target:
            with pytest.raises(grpc.aio.AioRpcError):
                await call(target, DENIED)

    with caplog.at_level(logging.INFO):
        run(scenario())

    (record,) = records(caplog)
    assert record.status == "PERMISSION_DENIED"


def test_a_handler_that_raises_is_logged_as_unknown_with_its_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        async with echo_server(ObservabilityInterceptor()) as target:
            with pytest.raises(grpc.aio.AioRpcError):
                await call(target, BOOM)

    with caplog.at_level(logging.INFO):
        run(scenario())

    (record,) = records(caplog)
    assert record.status == "UNKNOWN"
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None


def test_a_method_the_gateway_does_not_serve_is_still_accounted_for(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        async with echo_server(ObservabilityInterceptor()) as target:
            with pytest.raises(grpc.aio.AioRpcError):
                await call(target, UNKNOWN)

    with caplog.at_level(logging.INFO):
        run(scenario())

    (record,) = records(caplog)
    assert record.rpc == UNKNOWN
    assert record.status == "UNIMPLEMENTED"


def test_the_log_never_carries_the_metadata_a_call_arrived_with(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both secrets travel in metadata: the Gateway Token on the way in, and
    an Application Key would if anything ever put one there."""

    async def scenario() -> None:
        async with echo_server(ObservabilityInterceptor()) as target:
            await call(
                target,
                ECHO,
                metadata=(
                    ("authorization", f"Bearer {TOKEN}"),
                    ("hue-application-key", "an-application-key"),
                ),
            )

    with caplog.at_level(logging.DEBUG):
        run(scenario())

    assert TOKEN not in caplog.text
    assert "an-application-key" not in caplog.text


def test_a_client_hanging_up_mid_stream_is_not_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """gRPC closes the handler's generator when a client walks away. Logging
    that as a fault would bury real ones once the event stream lands."""

    async def scenario() -> None:
        async with echo_server(ObservabilityInterceptor()) as target:
            async with grpc.aio.insecure_channel(target) as channel:
                streaming = channel.unary_stream(SLEEP_STREAM)(b"30")
                await asyncio.sleep(0.1)
                streaming.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await streaming.read()
            await asyncio.sleep(0.1)

    with caplog.at_level(logging.INFO):
        run(scenario())

    (record,) = records(caplog)
    assert record.status == "CANCELLED"
    assert record.levelno < logging.ERROR


def test_the_correlation_metadata_key_is_one_grpc_will_carry() -> None:
    """gRPC rejects metadata keys that are not lowercase ASCII."""
    assert CORRELATION_ID_METADATA.islower()
    assert CORRELATION_ID_METADATA.isascii()


# --- the chain --------------------------------------------------------------


def test_a_rejected_call_is_logged_by_the_interceptor_outside_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Observability sits outermost so that an unauthenticated call, which
    never reaches a handler, still leaves a line behind."""

    async def scenario() -> None:
        async with echo_server(
            ObservabilityInterceptor(), AuthInterceptor(TOKEN), DeadlineInterceptor(5.0)
        ) as target:
            with pytest.raises(grpc.aio.AioRpcError):
                await call(target, ECHO)

    with caplog.at_level(logging.INFO):
        run(scenario())

    (record,) = records(caplog)
    assert record.status == "UNAUTHENTICATED"
    assert record.rpc == ECHO
