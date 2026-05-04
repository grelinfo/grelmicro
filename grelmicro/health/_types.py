"""Health Check Function Types."""

from collections.abc import Awaitable, Callable

from grelmicro._json import JSONEncodable

type HealthDetails = dict[str, JSONEncodable]
"""Per-check details payload. JSON-serializable dict keyed by string."""

type SyncHealthCheckFunc = Callable[[], HealthDetails | None]
"""Sync health check. Executed in a worker thread via ``asyncio.to_thread``."""

type AsyncHealthCheckFunc = Callable[[], Awaitable[HealthDetails | None]]
"""Async health check. Awaited directly."""

type HealthCheckFunc = SyncHealthCheckFunc | AsyncHealthCheckFunc
"""Any callable acceptable as a health check.

Returns:
- ``None``: healthy, no details.
- ``HealthDetails``: healthy, with a details dict.

Raises:
- ``HealthError``: unhealthy. The message surfaces in the response.
- Any other exception: unhealthy with a generic message. The
  traceback is logged server-side.
"""
