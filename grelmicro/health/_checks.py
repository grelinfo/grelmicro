"""Health Checks."""

import asyncio
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from logging import getLogger
from types import TracebackType
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from pydantic import BaseModel, NonNegativeFloat, PositiveFloat
from typing_extensions import Doc

from grelmicro._async import is_async_callable
from grelmicro._config import (
    Reconfigurable,
    default_env_prefix,
    resolve_config,
)
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
from grelmicro.health.errors import (
    HealthError,
    HealthSettingsValidationError,
)
from grelmicro.metrics import _emit

if TYPE_CHECKING:
    from grelmicro.providers._base import Provider

logger = getLogger("grelmicro.health")

# Check names are exposed via the ``?exclude=`` query parameter on
# ``/readyz`` and ``/healthz``. Restrict to a URL-safe, lower-case
# charset so the query-string matches the registered name byte-for-byte
# (no case folding, no whitespace trimming, no percent-encoding
# surprises). Colon is allowed for namespacing (e.g. "weather:circuitbreaker").
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9:_-]*$")
_NAME_MAX_LEN = 64


class HealthChecksConfig(BaseModel, frozen=True, extra="forbid"):
    """Health Checks Config."""

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


class HealthChecks(Reconfigurable[HealthChecksConfig]):
    """Manages health checks and runs them concurrently.

    Checks are plain async functions. Register them with the
    :meth:`check` decorator or the :meth:`add` method. All registered
    checks are executed in parallel via an ``asyncio.TaskGroup``. Each
    check has its own timeout (falling back to the default)
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

    kind: ClassVar[str] = "health"

    def __init__(
        self,
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. Multiple `HealthChecks` instances may
                coexist on one `Grelmicro` under different names.
                """,
            ),
        ] = "default",
        timeout: Annotated[
            PositiveFloat | None,
            Doc(
                """
                Default per-check timeout in seconds. Checks that
                exceed this duration are reported as ``error``.

                Default: 5.0. When unset and env reads are enabled (see ``env_load`` and
                ``GREL_ENV_LOAD``), resolves from the
                environment variable ``GREL_HEALTH_TIMEOUT`` (or
                ``GREL_HEALTH_{NAME_UPPER}_TIMEOUT`` for a named instance)
                if present, otherwise falls back to the
                ``HealthChecksConfig`` default.
                """
            ),
        ] = None,
        cache_ttl: Annotated[
            NonNegativeFloat | None,
            Doc(
                """
                Per-check cache TTL in seconds. Set to 0 to disable.

                Default: 1.0. When unset and env reads are enabled (see ``env_load`` and
                ``GREL_ENV_LOAD``), resolves from the
                environment variable ``GREL_HEALTH_CACHE_TTL`` (or
                ``GREL_HEALTH_{NAME_UPPER}_CACHE_TTL`` for a named instance)
                if present, otherwise falls back to the
                ``HealthChecksConfig`` default.
                """
            ),
        ] = None,
        env_prefix: Annotated[
            str | None,
            Doc(
                """
                Override the auto-derived environment variable prefix.

                Default: ``GREL_HEALTH_`` for the default instance,
                ``GREL_HEALTH_{NAME_UPPER}_`` for a named one.
                """
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                """
                Whether to read environment variables.

                When None (the default), follow the process-wide
                ``GREL_ENV_LOAD`` flag. Pass True or False to
                override the flag for this construction.
                """
            ),
        ] = None,
        auto_health: Annotated[
            bool,
            Doc(
                """
                Register a `provider:{short_name}` readiness check for
                every `Provider` active on the app, on startup. Off by
                default. Each registered check is critical, so an
                unreachable backend fails `/readyz`. For finer control,
                leave this off and call `add_provider` per provider.
                """
            ),
        ] = False,
    ) -> None:
        """Initialize the health checks."""
        config = resolve_config(
            HealthChecksConfig,
            explicit=None,
            kwargs={"timeout": timeout, "cache_ttl": cache_ttl},
            env_prefix=env_prefix or default_env_prefix("HEALTH", name),
            env_load=env_load,
            error_type=HealthSettingsValidationError,
        )
        self._setup(config, name=name, auto_health=auto_health)

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            HealthChecksConfig,
            Doc(
                """
                The pre-built health checks configuration.

                Use this path when the configuration is assembled at
                startup from a settings tree (for example YAML, Vault,
                or a ``pydantic-settings`` aggregator). The
                environment path is bypassed and the config is used
                as-is.
                """
            ),
        ],
        *,
        name: Annotated[
            str,
            Doc("Registration name. Defaults to `'default'`."),
        ] = "default",
        auto_health: Annotated[
            bool,
            Doc(
                "Register a `provider:{short_name}` readiness check for "
                "every active `Provider` on startup. Off by default."
            ),
        ] = False,
    ) -> Self:
        """Construct a `HealthChecks` from a pre-built `HealthChecksConfig`."""
        instance = cls.__new__(cls)
        instance._setup(config, name=name, auto_health=auto_health)  # noqa: SLF001
        return instance

    def _setup(
        self,
        config: HealthChecksConfig,
        *,
        name: str = "default",
        auto_health: bool = False,
    ) -> None:
        """Wire the validated config and runtime deps onto the instance."""
        self._name = name
        self._config = config
        self._auto_health = auto_health
        self._reconfigure_lock = asyncio.Lock()
        self._entries: dict[str, _Entry] = {}

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    async def __aenter__(self) -> Self:
        """Open the health checks.

        When `auto_health` is on, register one `provider:{short_name}`
        check per Provider active on the app.
        """
        if self._auto_health:
            self._register_active_providers()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the health checks."""

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
                "default when omitted."
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
                f"{_NAME_MAX_LEN} chars. "
                f"Valid examples: 'redis', 'db-primary', "
                f"'weather:circuitbreaker'."
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

    def add_provider(
        self,
        provider: Annotated[
            "Provider",
            Doc("The provider whose built-in readiness check to register."),
        ],
        *,
        name: Annotated[
            str | None,
            Doc(
                "Check name suffix. Defaults to the provider's "
                "``short_name``, so the check is ``provider:redis``. "
                "Pass an explicit name to disambiguate two providers of "
                "the same vendor, e.g. ``name='sessions'`` registers "
                "``provider:sessions``."
            ),
        ] = None,
        critical: Annotated[
            bool,
            Doc(
                "Whether the check affects ``/readyz``. Critical by "
                "default: an unreachable backend fails readiness. Pass "
                "``critical=False`` for a degradable dependency such as "
                "a cache."
            ),
        ] = True,
        timeout: Annotated[
            PositiveFloat | None,
            Doc("Per-check timeout override. Falls back to the default."),
        ] = None,
    ) -> None:
        """Register a provider's built-in readiness check as ``provider:{name}``.

        Raises:
            ValueError: If the provider ships no readiness check, or the
                resulting name is already registered.
        """
        from grelmicro.providers._base import Provider  # noqa: PLC0415

        if type(provider).check is Provider.check:
            msg = (
                f"{type(provider).__name__} ships no readiness check. "
                f"Register a custom check with health.check(...) instead."
            )
            raise ValueError(msg)
        self.add(
            f"provider:{name or provider.short_name}",
            provider.check,
            critical=critical,
            timeout=timeout,
        )

    def _register_active_providers(self) -> None:
        """Register a critical check for every Provider active on the app.

        Called from `__aenter__` when `auto_health` is on. A provider with
        no readiness check is skipped. A provider already registered under
        any name (an explicit `add_provider`, or a second `__aenter__`) is
        left untouched, so the explicit registration wins and re-entry is
        idempotent. A `provider:{short_name}` name held by a different
        provider (two providers of the same vendor) is skipped with a
        warning, pointing at the explicit `add_provider(provider, name=...)`
        form.
        """
        from grelmicro._app import Grelmicro  # noqa: PLC0415
        from grelmicro.providers._base import Provider  # noqa: PLC0415

        for provider in Grelmicro.current().providers:
            if type(provider).check is Provider.check:
                continue
            check = provider.check
            if any(entry.func == check for entry in self._entries.values()):
                continue
            check_name = f"provider:{provider.short_name}"
            if check_name in self._entries:
                logger.warning(
                    "auto_health: %r is already registered, skipping %r. "
                    "Register it with "
                    "health.add_provider(provider, name=...) to give it a "
                    "distinct name.",
                    check_name,
                    provider,
                )
                continue
            self.add(check_name, check, critical=True)

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
    """Execute a single health check, timing it and emitting metrics.

    Emits ``grelmicro.health.check.up`` (1 healthy, 0 unhealthy) and
    ``grelmicro.health.check.duration`` (seconds). Both are no-ops when no
    `Metrics` component is active. The check name and critical flag are
    bounded attributes (registered names, not user input).
    """
    start = time.monotonic()
    result = await _run_check_inner(entry)
    elapsed = time.monotonic() - start
    healthy = result["status"] == HealthStatus.OK
    _emit.observe(
        "grelmicro.health.check.up",
        1 if healthy else 0,
        **{"check.name": entry.name, "critical": entry.critical},
    )
    _emit.record_duration(
        "grelmicro.health.check.duration",
        elapsed,
        **{
            "check.name": entry.name,
            "outcome": "success" if healthy else "error",
        },
    )
    return result


async def _run_check_inner(entry: _Entry) -> CheckResult:
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
                # User code raised TimeoutError, not the configured timeout.
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
    except Exception as exc:
        logger.exception(
            "Health check '%s' raised unexpectedly",
            entry.name,
        )
        return CheckResult(
            status=HealthStatus.ERROR,
            critical=entry.critical,
            error=f"{type(exc).__name__}: {exc}",
            details=None,
        )
