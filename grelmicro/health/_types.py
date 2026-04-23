"""Health Check Function Types."""

from collections.abc import Awaitable, Callable
from typing import TypeAlias

from grelmicro._json import JSONEncodable

HealthDetails: TypeAlias = dict[str, JSONEncodable]
"""Per-check details payload. JSON-serializable dict keyed by string."""

SyncHealthCheckFunc: TypeAlias = Callable[[], HealthDetails | None]
"""Sync health check. Executed in a worker thread via ``anyio.to_thread``."""

AsyncHealthCheckFunc: TypeAlias = Callable[[], Awaitable[HealthDetails | None]]
"""Async health check. Awaited directly."""

HealthCheckFunc: TypeAlias = SyncHealthCheckFunc | AsyncHealthCheckFunc
"""Any callable acceptable as a health check.

Returns:
- ``None``: healthy, no details.
- ``HealthDetails``: healthy, with a details dict.

Raises:
- ``HealthError``: unhealthy. The message surfaces in the response.
- Any other exception: unhealthy with a generic message. The
  traceback is logged server-side.
"""
