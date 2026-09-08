"""The gateway's listener: what it answers, and how it stops answering.

Issue #9's acceptance is that the server starts, health answers, and a client
can list its services through reflection. All three are here, over a real
socket, plus the things that are only true if the machinery around them is
wired correctly: the size limit, the token, TLS, and a shutdown that both
stops answering and lets go of work that is still running.
"""

from __future__ import annotations

import asyncio
import signal
import time
from pathlib import Path

import grpc
import pytest
from conftest import (
    ECHO_SERVICE,
    SLEEP,
    BridgeCerts,
    cancellations,
    echo_handlers,
    run,
)
from grpc_health.v1 import health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc

from hue_grpc.serving.config import GatewayConfig, TlsConfig
from hue_grpc.serving.serve import HEALTH_SERVICE, HostedService, running_gateway

SERVING = health_pb2.HealthCheckResponse.SERVING
NOT_SERVING = health_pb2.HealthCheckResponse.NOT_SERVING

#: A listener on whatever port is free, that stops the moment it is asked to.
#: The drain window is real behaviour with its own test; paying it in every
#: other test here would be seconds of waiting for nothing.
EPHEMERAL = GatewayConfig(port=0, shutdown_drain=0)


def echo_service() -> HostedService:
    """The stand-in service, registered the way issue #10's will be."""

    def register(server: grpc.aio.Server) -> None:
        server.add_generic_rpc_handlers(
            (grpc.method_handlers_generic_handler(ECHO_SERVICE, echo_handlers()),)
        )

    return HostedService(name=ECHO_SERVICE, register=register)


async def health_status(
    target: str,
    *,
    service: str = "",
    credentials: grpc.ChannelCredentials | None = None,
) -> int:
    channel = (
        grpc.aio.insecure_channel(target)
        if credentials is None
        else grpc.aio.secure_channel(target, credentials)
    )
    async with channel:
        stub = health_pb2_grpc.HealthStub(channel)
        response = await stub.Check(health_pb2.HealthCheckRequest(service=service))
        return int(response.status)


async def listed_services(target: str, *, metadata: object = None) -> set[str]:
    async with grpc.aio.insecure_channel(target) as channel:
        stub = reflection_pb2_grpc.ServerReflectionStub(channel)

        async def requests() -> object:
            yield reflection_pb2.ServerReflectionRequest(list_services="*")

        call = stub.ServerReflectionInfo(requests(), metadata=metadata)
        response = await call.read()
        call.cancel()
        return {service.name for service in response.list_services_response.service}


def test_the_gateway_starts_and_health_answers_serving() -> None:
    async def scenario() -> None:
        async with running_gateway(EPHEMERAL) as gateway:
            assert await health_status(f"127.0.0.1:{gateway.port}") == SERVING

    run(scenario())


def test_an_ephemeral_port_is_reported_back() -> None:
    """Port 0 asks the OS for one; nothing can connect without being told."""

    async def scenario() -> None:
        async with running_gateway(EPHEMERAL) as gateway:
            assert gateway.port > 0

    run(scenario())


def test_a_registered_service_is_healthy_by_name() -> None:
    async def scenario() -> None:
        async with running_gateway(EPHEMERAL, services=[echo_service()]) as gateway:
            target = f"127.0.0.1:{gateway.port}"

            assert await health_status(target, service=ECHO_SERVICE) == SERVING

    run(scenario())


def test_reflection_lists_the_services_on_a_loopback_listener() -> None:
    """`grpcurl -plaintext 127.0.0.1:50051 list`, without grpcurl."""

    async def scenario() -> None:
        async with running_gateway(EPHEMERAL, services=[echo_service()]) as gateway:
            listed = await listed_services(f"127.0.0.1:{gateway.port}")

            assert HEALTH_SERVICE in listed
            assert ECHO_SERVICE in listed

    run(scenario())


def test_reflection_can_be_turned_off_without_touching_anything_else() -> None:
    async def scenario() -> None:
        config = GatewayConfig(port=0, reflection=False)
        async with running_gateway(config) as gateway:
            with pytest.raises(grpc.aio.AioRpcError) as failure:
                await listed_services(f"127.0.0.1:{gateway.port}")

            assert failure.value.code() == grpc.StatusCode.UNIMPLEMENTED

    run(scenario())


def test_an_oversized_request_is_refused_rather_than_read() -> None:
    async def scenario() -> None:
        config = GatewayConfig(port=0, max_inbound_message_bytes=1024)
        async with running_gateway(config) as gateway:
            with pytest.raises(grpc.aio.AioRpcError) as failure:
                await health_status(f"127.0.0.1:{gateway.port}", service="x" * 4096)

            assert failure.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED

    run(scenario())


def test_a_configured_token_guards_the_services_but_not_health() -> None:
    """A supervisor asking whether the process is up must not be answered
    with a complaint about its credentials."""

    async def scenario() -> None:
        config = GatewayConfig(port=0, gateway_token="a-gateway-token")
        async with running_gateway(config) as gateway:
            target = f"127.0.0.1:{gateway.port}"

            assert await health_status(target) == SERVING

            with pytest.raises(grpc.aio.AioRpcError) as failure:
                await listed_services(target)
            assert failure.value.code() == grpc.StatusCode.UNAUTHENTICATED

            authorized = (("authorization", "Bearer a-gateway-token"),)
            assert HEALTH_SERVICE in await listed_services(target, metadata=authorized)

    run(scenario())


def test_tls_is_a_configuration_change_and_nothing_else(
    listener_certs: BridgeCerts, tmp_path: Path
) -> None:
    async def scenario() -> None:
        config = GatewayConfig(
            port=0,
            tls=TlsConfig(
                certificate_file=listener_certs.cert_file,
                private_key_file=listener_certs.key_file,
            ),
        )
        async with running_gateway(config) as gateway:
            credentials = grpc.ssl_channel_credentials(
                root_certificates=listener_certs.ca_file.read_bytes()
            )

            status = await health_status(
                f"127.0.0.1:{gateway.port}", credentials=credentials
            )

            assert status == SERVING

    run(scenario())


def test_a_plaintext_client_cannot_talk_to_a_tls_listener(
    listener_certs: BridgeCerts,
) -> None:
    async def scenario() -> None:
        config = GatewayConfig(
            port=0,
            tls=TlsConfig(
                certificate_file=listener_certs.cert_file,
                private_key_file=listener_certs.key_file,
            ),
        )
        async with running_gateway(config) as gateway:
            with pytest.raises(grpc.aio.AioRpcError):
                await health_status(f"127.0.0.1:{gateway.port}")

    run(scenario())


def test_health_reports_not_serving_before_the_port_goes_away() -> None:
    """A load balancer needs to be told to stop sending work before the door
    is shut, or the calls in between are simply lost."""

    async def scenario() -> None:
        async with running_gateway(EPHEMERAL) as gateway:
            target = f"127.0.0.1:{gateway.port}"
            assert await health_status(target) == SERVING

            await gateway.begin_shutdown()

            assert await health_status(target) == NOT_SERVING

    run(scenario())


def test_leaving_the_gateway_running_is_not_an_option() -> None:
    async def scenario() -> None:
        async with running_gateway(EPHEMERAL) as gateway:
            target = f"127.0.0.1:{gateway.port}"

        with pytest.raises(grpc.aio.AioRpcError) as failure:
            await health_status(target)
        assert failure.value.code() == grpc.StatusCode.UNAVAILABLE

    run(scenario())


def test_shutdown_cancels_work_that_is_still_running() -> None:
    """The grace period is a bound, not a promise to wait: a Bridge request
    still in flight is cancelled along with the handler that made it, which is
    what stops the process exiting out from under an open connection."""
    cancellations.clear()

    async def scenario() -> None:
        config = GatewayConfig(port=0, shutdown_drain=0, shutdown_grace=0.1)
        started = time.perf_counter()
        async with running_gateway(config, services=[echo_service()]) as gateway:
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{gateway.port}")
            slow = channel.unary_unary(SLEEP)(b"30", timeout=30)
            # Let the call reach the handler before the door closes.
            await asyncio.sleep(0.2)

        with pytest.raises(grpc.aio.AioRpcError):
            await slow
        await channel.close()

        assert time.perf_counter() - started < 10

    run(scenario())

    assert cancellations == ["Sleep"]


def test_the_listener_keeps_accepting_while_it_drains() -> None:
    """Saying NOT_SERVING is only worth anything if a client can still connect
    to be told: otherwise it learns by failing to connect, which is the thing
    the health service exists to prevent."""

    async def scenario() -> None:
        config = GatewayConfig(port=0, shutdown_drain=1.0)

        async def ask_while_it_is_going_away(target: str) -> int:
            await asyncio.sleep(0.3)
            return await health_status(target)

        async with running_gateway(config) as gateway:
            asking = asyncio.ensure_future(
                ask_while_it_is_going_away(f"127.0.0.1:{gateway.port}")
            )

        # The block has exited, so health already says NOT_SERVING — and the
        # drain held the door open long enough for a connection made after
        # that to be told so, rather than refused.
        assert await asking == NOT_SERVING

    run(scenario())


def test_a_second_gateway_cannot_quietly_share_the_port() -> None:
    """gRPC turns SO_REUSEPORT on by default, so two half-configured services
    would each answer some of the calls."""

    async def scenario() -> None:
        async with running_gateway(EPHEMERAL) as gateway:
            taken = GatewayConfig(port=gateway.port)

            with pytest.raises(OSError, match="in use"):
                async with running_gateway(taken):
                    pass

    run(scenario())


def test_a_shutdown_signal_is_what_stops_it() -> None:
    """systemd stops the unit with SIGTERM and expects it to mean this."""

    async def scenario() -> None:
        async with running_gateway(EPHEMERAL) as gateway:
            waiting = asyncio.ensure_future(gateway.wait_for_shutdown_signal())
            await asyncio.sleep(0.1)
            signal.raise_signal(signal.SIGTERM)

            assert await asyncio.wait_for(waiting, timeout=5) == signal.SIGTERM

    run(scenario())
