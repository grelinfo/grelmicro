"""A real Redis Cluster and Valkey Cluster for the integration tier.

testcontainers has no cluster module, so one container runs three master
nodes. Each node listens on a host port bound one to one and announces
`127.0.0.1` with that port, so the addresses the cluster hands back to the
client are the ones the host reaches, with no address translation.

The fixtures are session scoped and start a cluster only when a test asks
for one, so each xdist worker that runs a cluster case starts one container
per server. Tests share it, so each test keeps to keys of its own, through a
unique prefix or unique names, and no test flushes the cluster.
"""

from __future__ import annotations

import random
import secrets
import socket
from collections.abc import Generator
from contextlib import closing, contextmanager

import pytest
from docker.errors import APIError
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.valkey import ValkeyProvider
from tests._containers import CONTAINER_LOG_TIMEOUT

REDIS_IMAGE = "redis:8"
VALKEY_IMAGE = "valkey/valkey:8.1"

NODES = 3
"""Master nodes per cluster, the fewest `--cluster create` accepts."""

READY = "Cluster ready"
"""The log line the start script writes once every slot is served."""

_BUS_OFFSET = 10000
"""Redis puts a node's cluster bus on its port plus this offset."""

_PORT_RANGE = (20000, 65535 - _BUS_OFFSET)
"""Host ports to draw from, low enough that each bus port stays valid."""

_ATTEMPTS = 5
"""Times to draw new ports when another process took one first."""

_SCRIPT = """\
set -e
for port in "$@"; do
  mkdir -p /data/$port
  {server} --port $port --cluster-enabled yes \
    --cluster-config-file /data/$port/nodes.conf \
    --cluster-announce-ip 127.0.0.1 --cluster-announce-port $port \
    --cluster-announce-bus-port $((port + {bus})) \
    --dir /data/$port --appendonly no --save "" --daemonize yes
done
sleep 1
nodes=""; for port in "$@"; do nodes="$nodes 127.0.0.1:$port"; done
yes yes | {cli} --cluster create $nodes --cluster-replicas 0
echo "{ready}"
tail -f /dev/null
"""


def free_ports(count: int) -> list[int]:
    """Return `count` distinct host ports that were free a moment ago.

    Each port is bound and released, so a port another process holds is
    skipped. No port is another one's cluster bus port. Another worker can
    still take one before the container binds it, which the caller handles
    by drawing again.
    """
    ports: list[int] = []
    while len(ports) < count:
        port = random.randint(*_PORT_RANGE)  # noqa: S311
        taken = {*ports, *(p + _BUS_OFFSET for p in ports)}
        taken |= {p - _BUS_OFFSET for p in ports}
        if port in taken:
            continue
        with closing(
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        ports.append(port)
    return ports


def _container(image: str, ports: list[int]) -> DockerContainer:
    """Build the container that runs one node per port."""
    valkey = "valkey" in image
    script = _SCRIPT.format(
        server="valkey-server" if valkey else "redis-server",
        cli="valkey-cli" if valkey else "redis-cli",
        bus=_BUS_OFFSET,
        ready=READY,
    )
    container = (
        DockerContainer(image)
        .with_kwargs(entrypoint="sh")
        .with_command(["-c", script, "sh", *map(str, ports)])
    )
    for port in ports:
        container = container.with_bind_ports(port, port)
    return container


@contextmanager
def start_cluster(image: str, scheme: str) -> Generator[str]:
    """Start a three-node cluster, and yield its URL with `scheme`.

    Ports are drawn again when the container cannot bind one, since another
    worker may take a port between the probe and the start.
    """
    for attempt in range(_ATTEMPTS):
        ports = free_ports(NODES)
        container = _container(image, ports)
        try:
            container.start()
        except APIError:
            container.stop()
            if attempt == _ATTEMPTS - 1:
                raise
            continue
        try:
            wait_for_logs(
                container,
                READY,
                timeout=CONTAINER_LOG_TIMEOUT,
                raise_on_exit=True,
            )
            yield f"{scheme}://127.0.0.1:{ports[0]}"
        finally:
            container.stop()
        return


@pytest.fixture(scope="session")
def redis_cluster_url() -> Generator[str]:
    """Start a Redis Cluster once per worker, and return its URL."""
    with start_cluster(REDIS_IMAGE, "redis+cluster") as url:
        yield url


@pytest.fixture(scope="session")
def valkey_cluster_url() -> Generator[str]:
    """Start a Valkey Cluster once per worker, and return its URL."""
    with start_cluster(VALKEY_IMAGE, "valkey+cluster") as url:
        yield url


CLUSTERS = ("redis-cluster", "valkey-cluster")
"""The backend names a suite gives its two cluster cases."""


def cluster_provider(
    request: pytest.FixtureRequest, name: str
) -> RedisProvider:
    """Return an unopened provider on the cluster a backend `name` stands for."""
    if name == "redis-cluster":
        return RedisProvider(request.getfixturevalue("redis_cluster_url"))
    return ValkeyProvider(request.getfixturevalue("valkey_cluster_url"))


def hash_tag_prefix() -> str:
    """Return a prefix with a unique hash tag, so one test's keys share a slot.

    The tag is new on every call, so tests sharing a cluster never see each
    other's keys.
    """
    return f"{{{secrets.token_hex(8)}}}:"


def unique_prefix() -> str:
    """Return a unique prefix with no hash tag, so keys spread over the slots.

    An adapter that touches one key per call takes it, so the test also
    proves the adapter needs no hash tag.
    """
    return f"{secrets.token_hex(8)}:"
