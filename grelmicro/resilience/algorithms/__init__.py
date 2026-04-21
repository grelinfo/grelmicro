"""Rate-limiter algorithms.

Algorithms are pure Pydantic configs (no logic). A
:class:`~grelmicro.resilience.RateLimiter` binds an algorithm to a
backend once at construction via
:meth:`~grelmicro.resilience._protocol.RateLimiterBackend.bind`; at
runtime the bound strategy is called directly, with no algorithm
dispatch on the hot path.
"""

from typing import Annotated

from pydantic import Discriminator

from grelmicro.resilience.algorithms.gcra import GCRA
from grelmicro.resilience.algorithms.tokenbucket import TokenBucket

Algorithm = Annotated[TokenBucket | GCRA, Discriminator("type")]
"""Discriminated union of supported rate-limiter algorithms."""

__all__ = ["GCRA", "Algorithm", "TokenBucket"]
