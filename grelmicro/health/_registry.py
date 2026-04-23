"""Health Check Registry."""

import inspect
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from logging import getLogger
from typing import Annotated

import anyio
from pydantic import BaseModel, NonNegativeFloat, PositiveFloat
from typing_extensions import Doc

from grelmicro.health._models import (
    CheckResult,
    HealthReport,
    HealthStatus,
)
from grelmicro.health._types import (
    AsyncHealthCheckFunc,
    HealthCheckFunc,
    HealthDetails,
)
from grelmicro.health.errors import HealthCheckTimeoutError, HealthError

logger = getLogger("grelmicro.health")


class HealthRegistryConfig(BaseModel, frozen=True, extra="forbid"):
    """Health Registry Config."""

    timeout: Annotated[
        PositiveFloat,
        Doc(
            "Default per-check timeout in seconds. Checks that exceed "
            "this duration are reported as ``error``. Can be "
            "overridden per check on registration."
        ),
    ] = 5.0
    cache_ttl: Annotated[
        NonNegativeFloat,
        Doc(
            "Per-check cache TTL in seconds. Each check's last "
            "result is reused until it is older than ``cache_ttl``. "
            "Concurrent calls coalesce via single-flight. Set to 0 "
            "to disable caching."
        ),
    ] = 1.0


@dataclass(slots=True)
class _Entry:
    """Registered check with its metadata and per-check cache slot."""

    name: str
    func: AsyncHealthCheckFunc  # always async after normalization
    critical: bool
    timeout: float
    cached_result: CheckResult | None = None
    cached_at: float = 0.0
    inflight: anyio.Event | None = field(default=None)


def _normalize(func: HealthCheckFunc) -> AsyncHealthCheckFunc:
    """Return an async callable. Sync funcs are wrapped via ``to_thread``.

    The sync/async decision is made once at registration, not per call.
    Handles both plain coroutine functions and callable instances whose
    ``__call__`` is async.
    """
    # We use ``__call__`` directly (not ``callable()``) because we
    # need to inspect whether the dunder itself is a coroutine
    # function, not just whether the object is callable.
    call = getattr(func, "__call__", None)  # noqa: B004
    if inspect.iscoroutinefunction(func) or (
        call is not None and inspect.iscoroutinefunction(call)
    ):
        return func  # type: ignore[return-value]  # ty: ignore[invalid-return-type]

    sync_func: Callable[[], HealthDetails | None] = func  # type: ignore[assignment]  # ty: ignore[invalid-assignment]

    async def _async_wrapper() -> HealthDetails | None:
        return await anyio.to_thread.run_sync(sync_func)  # ty: ignore[unresolved-attribute]

    return _async_wrapper


class HealthRegistry:
    """Registry that manages health checks and runs them concurrently.

    Checks are plain async functions. Register them with the
    :meth:`check` decorator or the :meth:`add` method. All registered
    checks are executed in parallel via an ``anyio`` task group. Each
    check has its own timeout (falling back to the registry default)
    and its own cached result. Concurrent requests for the same check
    share a single execution via an ``anyio.Event``.
    """

    def __init__(
        self,
        *,
        timeout: Annotated[
            PositiveFloat,
            Doc(
                "Default per-check timeout in seconds. Checks that "
                "exceed this duration are reported as ``error``."
            ),
        ] = 5.0,
        cache_ttl: Annotated[
            NonNegativeFloat,
            Doc("Per-check cache TTL in seconds. Set to 0 to disable."),
        ] = 1.0,
        auto_register: Annotated[
            bool,
            Doc(
                "Automatically register this instance as the global "
                "health registry singleton. Set to False for testing."
            ),
        ] = True,
    ) -> None:
        """Initialize the health registry."""
        self._config = HealthRegistryConfig(
            timeout=timeout, cache_ttl=cache_ttl
        )
        self._entries: dict[str, _Entry] = {}
        if auto_register:
            from grelmicro.health._backends import (  # noqa: PLC0415
                health_registry,
            )

            health_registry.set(self)

    def add(
        self,
        name: Annotated[str, Doc("Unique name identifying this check.")],
        func: Annotated[
            HealthCheckFunc,
            Doc(
                "Async function: returns ``None`` or a details dict "
                "on success, raises on failure."
            ),
        ],
        *,
        critical: Annotated[
            bool,
            Doc(
                "Whether this check affects the aggregate status and "
                "HTTP response code. Critical failures flip the "
                "aggregate to ``error`` and cause ``/readyz`` / "
                "``/healthz`` to return 503. Non-critical failures "
                "are visible in the ``/healthz`` body but do not flip "
                "the aggregate."
            ),
        ] = True,
        timeout: Annotated[
            PositiveFloat | None,
            Doc(
                "Per-check timeout override. Falls back to the "
                "registry default when omitted."
            ),
        ] = None,
    ) -> None:
        """Register a health check function.

        Raises:
            ValueError: If a check with the same name is already
                registered.
        """
        if name in self._entries:
            msg = f"Health check '{name}' is already registered"
            raise ValueError(msg)
        self._entries[name] = _Entry(
            name=name,
            func=_normalize(func),
            critical=critical,
            timeout=timeout if timeout is not None else self._config.timeout,
        )
        self._entries = dict(sorted(self._entries.items()))

    def check(
        self,
        name: Annotated[str, Doc("Unique name identifying this check.")],
        *,
        critical: Annotated[
            bool,
            Doc("Whether this check affects the aggregate status."),
        ] = True,
        timeout: Annotated[
            PositiveFloat | None,
            Doc("Per-check timeout override."),
        ] = None,
    ) -> Callable[[HealthCheckFunc], HealthCheckFunc]:
        """Decorate an async function to register it as a health check.

        Example:
            >>> @registry.check("database")
            ... async def check_db() -> dict | None:
            ...     return None
        """

        def decorator(func: HealthCheckFunc) -> HealthCheckFunc:
            self.add(name, func, critical=critical, timeout=timeout)
            return func

        return decorator

    async def run(
        self,
        *,
        critical_only: Annotated[
            bool,
            Doc("If True, only run critical checks."),
        ] = False,
        exclude: Annotated[
            Iterable[str] | None,
            Doc("Check names to skip."),
        ] = None,
    ) -> HealthReport:
        """Run the selected checks concurrently and aggregate.

        Each check runs with its own timeout. Results are cached per
        check for ``cache_ttl`` seconds. Concurrent calls for the
        same check coalesce via single-flight.

        Returns:
            A HealthReport with the aggregate status and per-check
            results.
        """
        excluded = set(exclude or ())
        selected = [
            (name, entry)
            for name, entry in self._entries.items()
            if name not in excluded and (not critical_only or entry.critical)
        ]

        results: dict[str, CheckResult] = {}

        async def _run(name: str, entry: _Entry) -> None:
            results[name] = await self._get_or_run(entry)

        async with anyio.create_task_group() as tg:
            for name, entry in selected:
                tg.start_soon(_run, name, entry)

        ordered = {name: results[name] for name, _ in selected}
        return HealthReport(
            status=self._aggregate_status(ordered.values()),
            checks=ordered,
        )

    async def _get_or_run(self, entry: _Entry) -> CheckResult:
        """Return a cached or freshly computed result for one check.

        Respects ``cache_ttl`` and serializes concurrent calls via a
        shared ``anyio.Event``.
        """
        ttl = self._config.cache_ttl
        now = time.monotonic()
        if (
            ttl > 0
            and entry.cached_result is not None
            and now - entry.cached_at < ttl
        ):
            return entry.cached_result

        if entry.inflight is not None:
            await entry.inflight.wait()
            if entry.cached_result is None:  # pragma: no cover
                # Invariant: the single-flight leader always writes
                # ``cached_result`` before calling ``event.set()``.
                msg = f"single-flight leader produced no result for '{entry.name}'"
                raise RuntimeError(msg)
            return entry.cached_result

        event = anyio.Event()
        entry.inflight = event
        try:
            result = await _run_check(entry)
            entry.cached_result = result
            entry.cached_at = time.monotonic()
            return result
        finally:
            entry.inflight = None
            event.set()

    @staticmethod
    def _aggregate_status(results: Iterable[CheckResult]) -> HealthStatus:
        """Aggregate per-check results into an overall status.

        Binary rule: ``error`` if any critical check failed, otherwise
        ``ok``. Non-critical failures are visible per-check but never
        flip the aggregate.
        """
        for result in results:
            if result["status"] == HealthStatus.ERROR and result["critical"]:
                return HealthStatus.ERROR
        return HealthStatus.OK


async def _run_check(entry: _Entry) -> CheckResult:
    """Execute a single health check function, returning a CheckResult.

    ``entry.func`` is always async (sync checks were wrapped at
    registration). No per-call branching.
    """
    try:
        with anyio.move_on_after(entry.timeout) as cancel_scope:
            result: HealthDetails | None = await entry.func()
        if cancel_scope.cancelled_caught:
            logger.warning(
                "Health check '%s' timed out after %gs",
                entry.name,
                entry.timeout,
            )
            error = HealthCheckTimeoutError(
                name=entry.name, timeout=entry.timeout
            )
            return CheckResult(
                status=HealthStatus.ERROR,
                critical=entry.critical,
                error=str(error),
                details=None,
            )
        return CheckResult(
            status=HealthStatus.OK,
            critical=entry.critical,
            error=None,
            details=result,
        )
    except HealthError as exc:
        logger.warning(
            "Health check '%s' reported unhealthy",
            entry.name,
            exc_info=exc,
        )
        return CheckResult(
            status=HealthStatus.ERROR,
            critical=entry.critical,
            error=str(exc),
            details=exc.details,
        )
    except Exception:
        logger.exception(
            "Health check '%s' raised unexpectedly",
            entry.name,
        )
        return CheckResult(
            status=HealthStatus.ERROR,
            critical=entry.critical,
            error="Health check failed",
            details=None,
        )
