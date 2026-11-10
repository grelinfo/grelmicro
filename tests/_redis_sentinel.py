"""A real Redis Sentinel set for the integration tier.

A master, one replica and three Sentinels share one Docker network. Every
port is bound one to one on the host, so the address Sentinel announces is
the one the host reaches once the client rewrites the host to `127.0.0.1`.

The `redis_sentinel` fixture is session scoped and starts a set only when a
test asks for one. Tests share it, so every adapter under test takes a unique
prefix, and no test flushes it. A failover test starts its own set with
`start_sentinel_set`, so the shared one is never failed over.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

import pytest
import redis
from docker.errors import APIError
from redis.asyncio import Redis
from redis.asyncio.sentinel import Sentinel
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network
from testcontainers.core.waiting_utils import wait_for_logs

from grelmicro.providers.redis import RedisProvider
from tests._containers import CONTAINER_LOG_TIMEOUT
from tests._redis_cluster import free_ports

IMAGE = "redis:8"

SERVICE = "mymaster"
"""The name the Sentinels monitor the master under."""

MASTER_ALIAS = "sentinel-master"
"""The master's name on the Docker network."""

SENTINELS = 3
"""Sentinels in a set, the fewest that keep a majority with one down."""

PROMOTION_TIMEOUT = 60.0
"""Seconds a test waits for Sentinel to promote the replica.

Sentinel marks the master down after one second. An election that finds no
replica fit to promote is tried again after ten seconds, so the bound covers
a few tries.
"""

_ATTEMPTS = 5
"""Times to draw new ports when another process took one first."""

_SENTINEL_SCRIPT = """\
cat > /tmp/sentinel.conf <<CONF
port $1
sentinel resolve-hostnames yes
sentinel monitor {service} {alias} $2 2
sentinel down-after-milliseconds {service} 1000
sentinel failover-timeout {service} 5000
sentinel parallel-syncs {service} 1
CONF
exec redis-sentinel /tmp/sentinel.conf
"""
"""Write a config the Sentinel can rewrite, then start it."""


@dataclass
class SentinelSet:
    """A running Sentinel set, and the containers a failover test controls.

    The containers are `None` on a set the test did not start, such as one a
    local run points the fixture at.
    """

    sentinel_ports: tuple[int, ...]
    master_port: int
    replica_port: int
    master: DockerContainer | None = None
    replica: DockerContainer | None = None

    def sentinel(self) -> Sentinel:
        """Return a Sentinel client that reaches the master from the host."""
        return Sentinel(
            [("localhost", port) for port in self.sentinel_ports],
            force_master_ip="127.0.0.1",
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
        )

    def provider(self) -> RedisProvider:
        """Return an unopened provider on the master Sentinel resolves."""
        client = self.sentinel().master_for(SERVICE)
        return RedisProvider.from_client(client, own=True)

    def kill_master(self) -> None:
        """Kill the master, as a crash would."""
        assert self.master is not None
        self.master.get_wrapped_container().kill()

    async def cut_replica(self, client: Redis) -> None:
        """Make the next writes on the master never reach the replica.

        The replica is paused, then the master drops its link. A paused
        replica still takes bytes into its socket buffer, so the link has to
        go too. The paused replica cannot connect again, so writes from here
        stay on the master alone.
        """
        assert self.replica is not None
        self.replica.get_wrapped_container().pause()
        await client.execute_command("CLIENT", "KILL", "TYPE", "replica")

    def resume_replica(self) -> None:
        """Let the replica run again after `cut_replica`."""
        assert self.replica is not None
        self.replica.get_wrapped_container().unpause()

    async def wait_for_promotion(
        self, sentinel: Sentinel, client: Redis
    ) -> None:
        """Wait until Sentinel names the replica as master and it answers."""
        deadline = time.monotonic() + PROMOTION_TIMEOUT
        while True:
            try:
                _host, port = await sentinel.discover_master(SERVICE)
                if port == self.replica_port and await client.ping():
                    return
            except RedisConnectionError, RedisTimeoutError:
                pass
            assert time.monotonic() < deadline, "the replica was not promoted"
            await asyncio.sleep(0.2)


def _server(network: Network, port: int, *args: str) -> DockerContainer:
    """Build a Redis server on `port`, bound to the same port on the host."""
    return (
        DockerContainer(IMAGE)
        .with_command(
            ["redis-server", "--port", str(port), "--save", "", *args]
        )
        .with_network(network)
        .with_bind_ports(port, port)
    )


def _sentinel(network: Network, port: int, master_port: int) -> DockerContainer:
    """Build a Sentinel on `port` that monitors the master."""
    script = _SENTINEL_SCRIPT.format(service=SERVICE, alias=MASTER_ALIAS)
    return (
        DockerContainer(IMAGE)
        .with_kwargs(entrypoint="sh")
        .with_command(["-c", script, "sh", str(port), str(master_port)])
        .with_network(network)
        .with_bind_ports(port, port)
    )


def _start(stack: ExitStack, container: DockerContainer, line: str) -> None:
    """Start `container`, stop it with the stack, and wait for `line`."""
    stack.callback(container.stop)
    container.start()
    wait_for_logs(
        container,
        re.escape(line),
        timeout=CONTAINER_LOG_TIMEOUT,
        raise_on_exit=True,
    )


def _sentinel_master(client: redis.Redis) -> dict[str, str]:
    """Return what a Sentinel knows of the master, as text.

    Sent raw and read here, since `sentinel_master()` on redis-py 6.0 passes
    an argument its own reply parser does not take.
    """
    reply = client.execute_command("SENTINEL", "MASTER", SERVICE)
    if isinstance(reply, dict):
        return {str(key): str(value) for key, value in reply.items()}
    return dict(zip(reply[::2], reply[1::2], strict=True))


def _wait_for_quorum(port: int) -> None:
    """Wait until the Sentinel on `port` sees the replica and the others.

    A Sentinel that does not know the replica cannot promote it, and one that
    does not know the other Sentinels cannot win the election.
    """
    deadline = time.monotonic() + CONTAINER_LOG_TIMEOUT
    with redis.Redis(
        port=port, socket_timeout=1.0, decode_responses=True
    ) as client:
        while True:
            try:
                state = _sentinel_master(client)
                if (
                    int(state["num-slaves"]) >= 1
                    and int(state["num-other-sentinels"]) == SENTINELS - 1
                ):
                    return
            except RedisConnectionError, RedisTimeoutError:
                pass
            assert time.monotonic() < deadline, f"sentinel {port} not ready"
            time.sleep(0.2)


def _start_set(stack: ExitStack, ports: list[int]) -> SentinelSet:
    """Start a set on `ports`: master, replica, then the Sentinels."""
    master_port, replica_port, *sentinel_ports = ports
    network = stack.enter_context(Network())
    master = _server(
        network, master_port, "--appendonly", "no"
    ).with_network_aliases(MASTER_ALIAS)
    _start(stack, master, "Ready to accept connections")
    replica = _server(
        network,
        replica_port,
        "--appendonly",
        "no",
        "--replicaof",
        MASTER_ALIAS,
        str(master_port),
    )
    _start(stack, replica, "MASTER <-> REPLICA sync: Finished with success")
    for port in sentinel_ports:
        _start(stack, _sentinel(network, port, master_port), "+monitor")
    for port in sentinel_ports:
        _wait_for_quorum(port)
    return SentinelSet(
        sentinel_ports=tuple(sentinel_ports),
        master_port=master_port,
        replica_port=replica_port,
        master=master,
        replica=replica,
    )


@contextmanager
def start_sentinel_set() -> Generator[SentinelSet]:
    """Start a Sentinel set, and yield it.

    Ports are drawn again when a container cannot bind one, since another
    worker may take a port between the probe and the start.
    """
    for attempt in range(_ATTEMPTS):
        with ExitStack() as stack:
            try:
                sentinel_set = _start_set(stack, free_ports(2 + SENTINELS))
            except APIError:
                if attempt == _ATTEMPTS - 1:
                    raise
                continue
            yield sentinel_set
        return


@pytest.fixture(scope="session")
def redis_sentinel() -> Generator[SentinelSet]:
    """Start a Sentinel set once per worker, shared and never failed over."""
    with start_sentinel_set() as sentinel_set:
        yield sentinel_set
