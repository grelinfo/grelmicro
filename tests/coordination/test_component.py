"""Tests for the Coordination component."""

import pytest

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination, LeaderElection
from grelmicro.coordination.memory import MemoryLeaderElectionBackend
from grelmicro.providers.redis import RedisProvider

pytestmark = [pytest.mark.timeout(1)]


def test_component_wraps_backend_instance() -> None:
    """A `LeaderElectionBackend` instance is used directly as the backend."""
    backend = MemoryLeaderElectionBackend()
    coordination = Coordination(backend)
    assert coordination.kind == "coordination"
    assert coordination.backend is backend


def test_component_builds_backend_from_provider() -> None:
    """A Provider is asked for its leader election backend."""
    coordination = Coordination(RedisProvider("redis://localhost:6379/0"))
    assert coordination.backend.__class__.__name__ == (
        "RedisLeaderElectionBackend"
    )


def test_leader_election_factory_binds_backend() -> None:
    """`coordination.leader_election(name)` binds the component's backend."""
    backend = MemoryLeaderElectionBackend()
    coordination = Coordination(backend)
    election = coordination.leader_election("worker")
    assert isinstance(election, LeaderElection)
    assert election.backend is backend


async def test_component_lifecycle_opens_and_closes_backend() -> None:
    """The component opens and closes its backend with the app."""
    backend = MemoryLeaderElectionBackend()
    micro = Grelmicro(uses=[Coordination(backend)])
    async with micro:
        assert micro.coordination.backend is backend
        election = micro.coordination.leader_election("worker")
        assert election.backend is backend
