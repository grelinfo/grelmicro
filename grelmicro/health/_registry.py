"""Health Check Registry."""

import asyncio
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from logging import getLogger
from types import TracebackType
from typing import Annotated, Self

from pydantic import BaseModel, NonNegativeFloat, PositiveFloat
from typing_extensions import Doc

from grelmicro._async import is_async_callable
from grelmicro._config import Reconfigurable, resolve_config
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
from grelmicro.health.errors import HealthError

logger = getLogger("grelmicro.health")

# Check names are exposed via the ``?exclude=`` query parameter on
# ``/readyz`` and ``/healthz``. Restrict to a URL-safe, lower-case
# charset so the query-string matches the registered name byte-for-byte
# (no case folding, no whitespace trimming, no percent-encoding
# surprises). Colon is allowed for namespacing (e.g. "weather:circuitbreaker").
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9:_-]*$")
_NAME_MAX_LEN = 64


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
    inflight: asyncio.Event | None = field(default=None)


def _normalize(func: HealthCheckFunc) -> AsyncHealthCheckFunc:
    """Return an async callable. Sync funcs are wrapped via ``to_thread``.

    The sync/async decision is made once at registration, not per call.
    """
    if is_async_callable(func):
        return func  # type: ignore[return-value]  # ty: ignore[invalid-return-type]

    sync_func: Callable[[], HealthDetails | None] = func  # type: ignore[assignment]  # ty: ignore[invalid-assignment]

    async def _async_wrapper() -> HealthDetails | None:
        return await asyncio.to_thread(sync_func)

    return _async_wrapper


class HealthRegistry(Reconfigurable[HealthRegistryConfig]):
    """Registry that manages health checks and runs them concurrently.

    Checks are plain async functions. Register them with the
    :meth:`check` decorator or the :meth:`add` method. All registered
    checks are executed in parallel via an ``asyncio.TaskGroup``. Each
    check has its own timeout (falling back to the registry default)
    and its own cached result. Concurrent requests for the same check
    share a single execution via an ``asyncio.Event``.

    Supports live reconfiguration via
    `reconfigure(new_config)`.
    A swap takes effect on the next :meth:`run`. In-flight rounds
    keep the ``cache_ttl`` they started with. The new default
    ``timeout`` applies to checks registered after the swap.
    Existing checks keep the timeout they were registered with.
    Re-register a check to pick up the new default. See
    [Live reconfiguration](../architecture/reconfigure.md).
    """

    def __init__(
        self,
        *,
        timeout: Annotated[
            PositiveFloat | None,
            Doc(
                """
                Default per-check timeout in seconds. Checks that
                exceed this duration are reported as ``error``.

                Default: 5.0. When unset and env reads are enabled (see ``read_env`` and
                ``GREL_CONFIG_FROM_ENV``), resolves from the
                environment variable ``GREL_HEALTH_TIMEOUT`` if
                present, otherwise falls back to the
                ``HealthRegistryConfig`` default.
                """
            ),
        ] = None,
        cache_ttl: Annotated[
            NonNegativeFloat | None,
            Doc(
                """
                Per-check cache TTL in seconds. Set to 0 to disable.

                Default: 1.0. When unset and env reads are enabled (see ``read_env`` and
                ``GREL_CONFIG_FROM_ENV``), resolves from the
                environment variable ``GREL_HEALTH_CACHE_TTL`` if
                present, otherwise falls back to the
                ``HealthRegistryConfig`` default.
                """
            ),
        ] = None,
        env_prefix: Annotated[
            str | None,
            Doc(
                """
                Override the auto-derived environment variable prefix.

                Default: ``GREL_HEALTH_``.
                """
            ),
        ] = None,
        read_env: Annotated[
            bool | None,
            Doc(
                """
                Whether to read environment variables.

                When None (the default), follow the process-wide
                ``GREL_CONFIG_FROM_ENV`` flag. Pass True or False to
                override the flag for this construction.
                """
            ),
        ] = None,
    ) -> None:
        """Initialize the health registry."""
        config = resolve_config(
            HealthRegistryConfig,
            explicit=None,
            kwargs={"timeout": timeout, "cache_ttl": cache_ttl},
            env_prefix=env_prefix or "GREL_HEALTH_",
            read_env=read_env,
        )
        self._setup(config)

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            HealthRegistryConfig,
            Doc(
                """
                The pre-built health registry configuration.

                Use this path when the configuration is assembled at
                startup from a settings tree (for example YAML, Vault,
                or a ``pydantic-settings`` aggregator). The
                environment path is bypassed and the config is used
                as-is.
                """
            ),
        ],
    ) -> Self:
        """Construct a `HealthRegistry` from a pre-built `HealthRegistryConfig`."""
        instance = cls.__new__(cls)
        instance._setup(config)  # noqa: SLF001
        return instance

    def _setup(self, config: HealthRegistryConfig) -> None:
        """Wire the validated config and runtime deps onto the instance."""
        self._config = config
        self._reconfigure_lock = asyncio.Lock()
        self._entries: dict[str, _Entry] = {}

    async def __aenter__(self) -> Self:
        """Open the health registry."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the health registry."""

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
            ValueError: If ``name`` is already registered, or does not
                match ``^[a-z0-9][a-z0-9:_-]*$`` (max 64 chars).
                Colon is allowed for namespacing, e.g.
                ``"weather:circuitbreaker"``.
        """
        config = self._config
        if (
            not name
            or len(name) > _NAME_MAX_LEN
            or not _NAME_PATTERN.match(name)
        ):
            msg = (
                f"Invalid health check name {name!r}: must match "
                f"^[a-z0-9][a-z0-9:_-]*$ and be at most "
                f"{_NAME_MAX_LEN} chars"
            )
            raise ValueError(msg)
        if name in self._entries:
            msg = f"Health check '{name}' is already registered"
            raise ValueError(msg)
        self._entries[name] = _Entry(
            name=name,
            func=_normalize(func),
            critical=critical,
            timeout=timeout if timeout is not None else config.timeout,
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
        config = self._config
        if exclude is None:
            excluded: frozenset[str] = frozenset()
        else:
            excluded = frozenset(exclude)
        selected = [
            (name, entry)
            for name, entry in self._entries.items()
            if name not in excluded and (not critical_only or entry.critical)
        ]

        if not selected:
            return HealthReport(status=HealthStatus.OK, checks={})

        results: dict[str, CheckResult] = {}

        async def _run(name: str, entry: _Entry) -> None:
            results[name] = await self._get_or_run(
                entry, cache_ttl=config.cache_ttl
            )

        async with asyncio.TaskGroup() as tg:
            for name, entry in selected:
                tg.create_task(_run(name, entry))

        ordered = {name: results[name] for name, _ in selected}
        return HealthReport(
            status=self._aggregate_status(ordered.values()),
            checks=ordered,
        )

    async def _get_or_run(
        self, entry: _Entry, *, cache_ttl: float
    ) -> CheckResult:
        """Return a cached or freshly computed result for one check.

        Respects ``cache_ttl`` and serializes concurrent calls via a
        shared ``asyncio.Event``. If the single-flight leader is
        cancelled before it produces a result, waiters take the lead
        themselves instead of failing. The caller captures
        ``cache_ttl`` from a snapshot at the start of the round so a
        concurrent ``reconfigure`` cannot change the cache decision
        mid-call.
        """
        ttl = cache_ttl
        while True:
            now = time.monotonic()
            if (
                ttl > 0
                and entry.cached_result is not None
                and now - entry.cached_at < ttl
            ):
                return entry.cached_result

            if entry.inflight is not None:
                await entry.inflight.wait()
                # Leader may have been cancelled before writing a result: loop.
                continue

            event = asyncio.Event()
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
        try:
            async with asyncio.timeout(entry.timeout) as cm:
                result: HealthDetails | None = await entry.func()
        except TimeoutError:
            if not cm.expired():
                # User code raised TimeoutError, not the registry timeout.
                raise
            logger.warning(
                "Health check '%s' timed out after %gs",
                entry.name,
                entry.timeout,
            )
            return CheckResult(
                status=HealthStatus.ERROR,
                critical=entry.critical,
                error=(
                    f"Health check '{entry.name}' timed out "
                    f"after {entry.timeout:g}s"
                ),
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
