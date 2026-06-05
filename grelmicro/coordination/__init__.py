"""Coordination primitives for distributed role assignment."""

from grelmicro.coordination._component import Coordination
from grelmicro.coordination.abc import LeaderElectionBackend, LeaderRecord
from grelmicro.coordination.leaderelection import (
    LeaderElection,
    LeaderElectionConfig,
)

__all__ = [
    "Coordination",
    "LeaderElection",
    "LeaderElectionBackend",
    "LeaderElectionConfig",
    "LeaderRecord",
]
