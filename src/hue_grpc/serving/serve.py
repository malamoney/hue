"""Standing the listener up, and taking it down again.

What is here is only the machinery: the listener, the health service,
reflection, the interceptor chain, and shutdown. The services themselves
arrive from the outside, so that the thing which knows how to reach a Bridge
and the thing which knows how to run a server never have to know about each
other.

Shutdown is three steps in a fixed order. Health flips to `NOT_SERVING`
first, so anything watching or asking is told to stop sending work. The
listener keeps accepting for `shutdown_drain` seconds, which is what makes
saying so first worth anything — without the pause a client learns the gateway
is going away by failing to connect, which is the outcome the health service
exists to avoid. Then it stops accepting, and calls already in flight have
`shutdown_grace` seconds to finish. Past that they are cancelled, and that
cancellation is what unwinds a Bridge request still waiting for an answer
rather than the process exiting out from under it.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection

from hue_grpc.logs import fields
from hue_grpc.serving.config import GatewayConfig
from hue_grpc.serving.interceptors import (
    AuthInterceptor,
    DeadlineInterceptor,
    ObservabilityInterceptor,
)

__all__ = [
    "HEALTH_SERVICE",
    "REFLECTION_SERVICE",
    "SHUTDOWN_SIGNALS",
    "HostedService",
    "RunningGateway",
    "running_gateway",
    "serve",
]

#: `grpc.health.v1.Health`, the standard health service every gRPC client and
#: every supervisor already knows how to call.
HEALTH_SERVICE: str = health.SERVICE_NAME

#: `grpc.reflection.v1alpha.ServerReflection`.
REFLECTION_SERVICE: str = reflection.SERVICE_NAME

#: What systemd sends to stop the unit, and what Ctrl-C sends in a terminal.
SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HostedService:
    """One gRPC service to host, and the name health and reflection know it by.

    A pair rather than a servicer object because the generated
    `add_*Servicer_to_server` functions are the only way to register one, and
    they are free functions.

    Not a Hue Resource's service in `CONTEXT.md`'s sense — nothing here knows
    what a Light is.
    """

    name: str
    register: Callable[[grpc.aio.Server], None]


class RunningGateway:
    """A gateway that is listening. Handed out by `running_gateway`."""

    def __init__(
        self,
        *,
        config: GatewayConfig,
        port: int,
        server: grpc.aio.Server,
        health_servicer: health.aio.HealthServicer,
    ) -> None:
        self.config = config
        #: The port actually bound, which is not `config.port` when that was 0.
        self.port = port
        self._server = server
        self._health = health_servicer
        self._shutting_down = False

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    async def begin_shutdown(self) -> None:
        """Say `NOT_SERVING` to anyone who asks, while still answering calls.

        `running_gateway` calls this on the way out, and waits
        `shutdown_drain` seconds before the listener stops accepting.

        Idempotent, and permanent: the health service refuses to go back to
        `SERVING` afterwards, because a gateway that is on its way out has
        nothing to gain from being sent more work.
        """
        if self._shutting_down:
            return
        self._shutting_down = True
        _log.info("gateway entering graceful shutdown", **fields(port=self.port))
        await self._health.enter_graceful_shutdown()

    async def wait_for_shutdown_signal(
        self, signals: Sequence[signal.Signals] = SHUTDOWN_SIGNALS
    ) -> signal.Signals:
        """Block until one of `signals` arrives, and say which it was."""
        loop = asyncio.get_running_loop()
        received: asyncio.Future[signal.Signals] = loop.create_future()

        def deliver(number: signal.Signals) -> None:
            if not received.done():
                received.set_result(number)

        for number in signals:
            loop.add_signal_handler(number, deliver, number)
        try:
            return await received
        finally:
            for number in signals:
                loop.remove_signal_handler(number)


def _server_options(config: GatewayConfig) -> list[tuple[str, Any]]:
    return [
        ("grpc.max_receive_message_length", config.max_inbound_message_bytes),
        ("grpc.max_send_message_length", config.max_outbound_message_bytes),
        # gRPC turns SO_REUSEPORT on by default. Left on, a second gateway
        # started by mistake binds the same port and silently takes a share of
        # the calls; off, it fails to start and says why.
        ("grpc.so_reuseport", 0),
    ]


def _interceptors(config: GatewayConfig) -> list[grpc.aio.ServerInterceptor]:
    """Outside-in. See `hue_grpc.serving.interceptors` for why this order."""
    return [
        ObservabilityInterceptor(),
        AuthInterceptor(
            config.gateway_token, exempt_services=frozenset({HEALTH_SERVICE})
        ),
        DeadlineInterceptor(config.default_deadline),
    ]


def _bind(server: grpc.aio.Server, config: GatewayConfig) -> int:
    """Listen, or say plainly why not.

    gRPC reports a refused bind as a `RuntimeError` naming an environment
    variable to set — true, and useless to a service that has already failed
    to start. The address is almost always taken, so say so, as the kind of
    error everything else that fails to bind a socket raises.
    """
    target = config.listen_target
    try:
        if config.tls is None:
            port = server.add_insecure_port(target)
        else:
            certificate_chain, private_key = config.tls.read()
            port = server.add_secure_port(
                target,
                grpc.ssl_server_credentials(((private_key, certificate_chain),)),
            )
    except RuntimeError as refused:
        raise OSError(
            f"could not listen on {target}: address already in use, or not "
            f"an address this host has"
        ) from refused
    return int(port)


@asynccontextmanager
async def running_gateway(
    config: GatewayConfig, services: Sequence[HostedService] = ()
) -> AsyncIterator[RunningGateway]:
    """A listening gateway for as long as the block runs, then a clean stop."""
    health_servicer = health.aio.HealthServicer()
    server = grpc.aio.server(
        interceptors=_interceptors(config), options=_server_options(config)
    )
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    for service in services:
        service.register(server)

    if config.reflection_enabled:
        reflection.enable_server_reflection(
            [HEALTH_SERVICE, REFLECTION_SERVICE, *(s.name for s in services)], server
        )

    port = _bind(server, config)
    await server.start()
    # Said outright rather than left to the servicer's constructor, which
    # happens to seed the overall status as SERVING before anything is
    # listening. What this gateway reports is this gateway's to state.
    serving = health_pb2.HealthCheckResponse.SERVING
    await health_servicer.set(health.OVERALL_HEALTH, serving)
    for service in services:
        await health_servicer.set(service.name, serving)

    gateway = RunningGateway(
        config=config, port=port, server=server, health_servicer=health_servicer
    )
    _log.info(
        "gateway listening",
        **fields(
            address=config.address,
            port=port,
            tls=config.tls is not None,
            authenticated=config.gateway_token is not None,
            reflection=config.reflection_enabled,
            services=[HEALTH_SERVICE, *(s.name for s in services)],
        ),
    )
    try:
        yield gateway
    finally:
        await gateway.begin_shutdown()
        if config.shutdown_drain:
            await asyncio.sleep(config.shutdown_drain)
        await server.stop(config.shutdown_grace)
        _log.info("gateway stopped", **fields(port=port))


async def serve(config: GatewayConfig, services: Sequence[HostedService] = ()) -> None:
    """Listen until a shutdown signal arrives, then stop gracefully."""
    async with running_gateway(config, services) as gateway:
        received = await gateway.wait_for_shutdown_signal()
        _log.info("shutdown signal received", **fields(signal=received.name))
