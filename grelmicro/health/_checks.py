"""Health Checks."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from logging import getLogger
from math import ceil
from time import monotonic, monotonic_ns
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Literal,
    Self,
    cast,
    overload,
)
from warnings import deprecated

from pydantic import BaseModel, PositiveFloat, field_validator
from typing_extensions import Doc

from grelmicro._async import is_async_callable
from grelmicro._config import (
    Reconfigurable,
    default_env_prefix,
    match_case,
    resolve_config,
)
from grelmicro._duration import Retention, check_positive_wait, nanoseconds
from grelmicro._markers import Registered, mark_registered
from grelmicro.errors import SettingsValidationError
from grelmicro.health import _liveness
from grelmicro.health._liveness import Liveness, Watchdog
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
)
from grelmicro.metrics import _emit
from grelmicro.providers._base import Provider

if TYPE_CHECKING:
    from grelmicro.resilience.circuitbreaker import CircuitBreaker

logger = getLogger("grelmicro.health")

# Check names are exposed via the ``?exclude=`` query parameter on
# ``/readyz`` and ``/healthz``. Restrict to a URL-safe, lower-case
# charset so the query-string matches the registered name byte-for-byte
# (no case folding, no whitespace trimming, no percent-encoding
# surprises). Colon is allowed for namespacing (e.g. "weather:circuitbreaker").
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9:_-]*$")
_NAME_MAX_LEN = 64


def _valid_name(name: str) -> bool:
    """Return whether `name` is a valid check name."""
    return len(name) <= _NAME_MAX_LEN and _NAME_PATTERN.match(name) is not None


_NOT_SECONDS = "timeout must be a number of seconds"
"""The refusal of a timeout that is a bool or not a number."""


def _checked_timeout(value: object) -> float:
    """Return a check timeout of float seconds, refused under `timeout`.

    Raises:
        ValueError: If `value` is a bool or not a number, is not finite,
            or is zero or below. The message never repeats the value.
    """
    if isinstance(value, int | float) and not isinstance(value, bool):
        return check_positive_wait(value, "timeout")
    raise ValueError(_NOT_SECONDS)


type ProviderChecks = Literal["off", "report", "require"]
"""Which check `HealthChecks` registers for each Provider on the app.

`"off"` registers none. `"report"` registers a non-critical
`provider:{short_name}` check, shown in `/healthz` and left out of
`/readyz`. `"require"` registers a critical one, so a down backend fails
`/readyz`.
"""

_PROVIDER_CHECKS: tuple[ProviderChecks, ...] = ("off", "report", "require")
"""Every `ProviderChecks` mode, in the order a refusal lists them."""


def _resolve_provider_checks(
    provider_checks: ProviderChecks | None,
) -> ProviderChecks:
    """Return the `provider_checks=` mode, matched in any case.

    `None` reads as `"report"` and warns with `Grelmicro045Warning` at the
    caller of `HealthChecks` or `HealthChecks.from_config`.

    Raises:
        SettingsValidationError: If `provider_checks` names no mode.
    """
    if provider_checks is None:
        from grelmicro._deprecation import PROVIDER_CHECKS_NONE  # noqa: PLC0415

        PROVIDER_CHECKS_NONE.warn(stacklevel=3)
        return "report"
    spelled = match_case(ProviderChecks, provider_checks)
    if spelled not in _PROVIDER_CHECKS:
        msg = f"provider_checks= must be one of {', '.join(_PROVIDER_CHECKS)}"
        raise SettingsValidationError(msg)
    return spelled


class HealthChecksConfig(BaseModel, frozen=True, extra="forbid"):
    """Health Checks Config."""

    timeout: Annotated[
        float,
        Doc(
            "Default per-check timeout in seconds. Checks that exceed "
            "this duration are reported as ``error``. Can be "
            "overridden per check on registration."
        ),
    ] = 5.0
    cache_ttl: Annotated[
        Retention,
        Doc(
            "Per-check cache TTL, in whole seconds or as a `timedelta`. "
            "A float is refused. Each check's last result is reused "
            "until it is ``cache_ttl`` old. Concurrent calls coalesce "
            "via single-flight. Set to 0 to disable caching. From "
            "text, such as an environment variable, it reads whole "
            'seconds (`"60"`) or an ISO 8601 duration (`"PT0.5S"`).'
        ),
    ] = timedelta(seconds=1)

    @field_validator("timeout", mode="before")
    @classmethod
    def _refuse_bool_timeout(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse a bool timeout."""
        if not isinstance(value, bool):
            return value
        raise ValueError(_NOT_SECONDS)

    @field_validator("timeout")
    @classmethod
    def _check_timeout(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse a timeout that is not a finite number, or is zero or below."""
        return _checked_timeout(value)


@dataclass(slots=True)
class _Entry:
    """Registered check with its metadata and per-check cache slot."""

    name: str
    func: AsyncHealthCheckFunc  # always async after normalization
    critical: bool
    timeout: float
    liveness: bool = False
    cached_result: CheckResult | None = None
    cached_at: int = 0
    """When `cached_result` was stored, in monotonic nanoseconds."""
    inflight: asyncio.Event | None = field(default=None)


def _normalize(func: HealthCheckFunc) -> AsyncHealthCheckFunc:
    """Return an async callable. Sync funcs are wrapped via ``to_thread``.

    The sync/async decision is made once at registration, not per call.
    """
    if is_async_callable(func):
        return cast("AsyncHealthCheckFunc", func)

    sync_func = cast("Callable[[], HealthDetails | None]", func)

    async def _async_wrapper() -> HealthDetails | None:
        return await asyncio.to_thread(sync_func)

    return _async_wrapper


def _left_closed_by_fake(func: object) -> bool:
    """Return whether `func` probes a Provider `micro.fake()` left closed.

    A faked test connects to nothing, so a readiness check on a Provider the
    fake never opened is left out of the report instead of failing it.
    """
    from grelmicro.providers._base import Provider  # noqa: PLC0415

    provider = getattr(func, "__self__", None)
    return isinstance(provider, Provider) and provider._left_closed()  # noqa: SLF001


def _app_open() -> Callable[[], bool]:
    """Return a callable that reads whether the active app is open.

    It reads `app.opened` on each call, so it follows the app through every
    close and reopen. Without an active app, it always reads true.
    """
    from grelmicro._app import Grelmicro, NoActiveAppError  # noqa: PLC0415

    try:
        app = Grelmicro.current()
    except NoActiveAppError:
        return lambda: True
    return lambda: app.opened


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

    @overload
    def __init__(
        self,
        *,
        name: str = "default",
        timeout: PositiveFloat | None = None,
        cache_ttl: int | timedelta | None = None,
        liveness: Liveness | None = None,
        provider_checks: ProviderChecks = "report",
        env_prefix: str | None = None,
        env_load: bool | None = None,
    ) -> None: ...

    @overload
    @deprecated(
        "`provider_checks=None` is removed in 0.45.0. "
        'Use `provider_checks="off"` to register no provider checks, '
        'or omit the argument for `"report"` instead.',
        category=None,
    )
    def __init__(
        self,
        *,
        name: str = "default",
        timeout: PositiveFloat | None = None,
        cache_ttl: int | timedelta | None = None,
        liveness: Liveness | None = None,
        provider_checks: None,
        env_prefix: str | None = None,
        env_load: bool | None = None,
    ) -> None: ...

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
            int | timedelta | None,
            Doc(
                """
                Per-check cache TTL, in whole seconds or as a
                `timedelta`. A float is refused. Set to 0 to disable.

                Default: 1 second. When unset and env reads are enabled (see ``env_load`` and
                ``GREL_ENV_LOAD``), resolves from the
                environment variable ``GREL_HEALTH_CACHE_TTL`` (or
                ``GREL_HEALTH_{NAME_UPPER}_CACHE_TTL`` for a named instance)
                if present, otherwise falls back to the
                ``HealthChecksConfig`` default.
                """
            ),
        ] = None,
        liveness: Annotated[
            Liveness | None,
            Doc(
                """
                When the worker counts as stuck, and exits so it is
                replaced. `None` (the default) runs no liveness check and
                starts no watchdog, so `/livez` always answers `200`.
                """
            ),
        ] = None,
        provider_checks: Annotated[
            ProviderChecks | None,
            Doc(
                """
                Which check to register for each `Provider` active on the
                app, on startup, matched in any case. `"report"` (the
                default) registers a non-critical `provider:{short_name}`
                check, shown in `/healthz` and left out of `/readyz`.
                `"require"` registers a critical one, so a down backend
                fails `/readyz`, and needs an active app on startup. `"off"`
                registers none. A Provider already added with `add_provider`
                keeps that registration.

                `None` reads as `"report"` and warns with
                `Grelmicro045Warning`. It is removed in 0.45.0.
                """
            ),
        ] = "report",
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

                Pass False when the values here are the whole truth.
                Env reads fill every field not passed, so a config
                half taken from somewhere else silently gets the rest
                from the environment.
                """
            ),
        ] = None,
    ) -> None:
        """Initialize the health checks.

        Raises:
            SettingsValidationError: If `provider_checks` names no mode.
        """
        mode = _resolve_provider_checks(provider_checks)
        resolved_env_prefix = env_prefix or default_env_prefix("HEALTH", name)
        config = resolve_config(
            HealthChecksConfig,
            explicit=None,
            kwargs={"timeout": timeout, "cache_ttl": cache_ttl},
            env_prefix=resolved_env_prefix,
            env_load=env_load,
        )
        self._setup(config, name=name, provider_checks=mode, liveness=liveness)
        # Registers for external reload under `GREL_HEALTH_`. Only this path
        # tracks: `from_config` takes a pre-built config and stays static,
        # matching every other reconfigurable component.
        self._track_reconfigure(resolved_env_prefix)

    @overload
    @classmethod
    def from_config(
        cls,
        config: HealthChecksConfig,
        *,
        name: str = "default",
        provider_checks: ProviderChecks = "report",
        liveness: Liveness | None = None,
    ) -> Self: ...

    @overload
    @classmethod
    @deprecated(
        "`provider_checks=None` is removed in 0.45.0. "
        'Use `provider_checks="off"` to register no provider checks, '
        'or omit the argument for `"report"` instead.',
        category=None,
    )
    def from_config(
        cls,
        config: HealthChecksConfig,
        *,
        name: str = "default",
        provider_checks: None,
        liveness: Liveness | None = None,
    ) -> Self: ...

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
        provider_checks: Annotated[
            ProviderChecks | None,
            Doc(
                "Which check to register for each active `Provider` on "
                'startup, as in `HealthChecks`. Defaults to `"report"`. '
                '`None` reads as `"report"` and warns with '
                "`Grelmicro045Warning`. It is removed in 0.45.0."
            ),
        ] = "report",
        liveness: Annotated[
            Liveness | None,
            Doc(
                "When the worker counts as stuck, and exits so it is "
                "replaced. `None` (the default) starts nothing."
            ),
        ] = None,
    ) -> Self:
        """Construct a `HealthChecks` from a pre-built `HealthChecksConfig`.

        Raises:
            SettingsValidationError: If `provider_checks` names no mode.
        """
        mode = _resolve_provider_checks(provider_checks)
        instance = cls.__new__(cls)
        instance._setup(  # noqa: SLF001
            config, name=name, provider_checks=mode, liveness=liveness
        )
        return instance

    def _setup(
        self,
        config: HealthChecksConfig,
        *,
        name: str = "default",
        provider_checks: ProviderChecks = "report",
        liveness: Liveness | None = None,
    ) -> None:
        """Wire the validated config and runtime deps onto the instance."""
        self._name = name
        self._liveness = liveness
        self._liveness_failures = 0
        self._liveness_task: asyncio.Task[None] | None = None
        self._watchdog: Watchdog | None = None
        self._config = config
        self._provider_checks = provider_checks
        """The `provider_checks=` mode."""
        self._reconfigure_lock = asyncio.Lock()
        self._entries: dict[str, _Entry] = {}
        self._auto_registered: list[str] = []
        """Checks `provider_checks` added on this run, removed when it closes."""
        self._depth = 0
        """How many open scopes hold these checks, so only the last one
        to close drops what `provider_checks` added."""

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    @property
    def is_alive(self) -> bool:
        """Whether the last round of liveness checks passed.

        `/livez` answers `503` while this is false. It is also false once
        the liveness checks stopped on an unexpected error, which stops the
        worker. It is always true when no `Liveness` is set.
        """
        task = self._liveness_task
        died = (
            task is not None
            and task.done()
            and not task.cancelled()
            and task.exception() is not None
        )
        return self._liveness_failures == 0 and not died

    async def __aenter__(self) -> Self:
        """Open the health checks.

        With `provider_checks="report"` or `"require"`, register one
        `provider:{short_name}` check per Provider active on the app.

        Raises:
            NoActiveAppError: If `provider_checks` is `"require"` and no app
                is active.
        """
        self._depth += 1
        if self._provider_checks in ("report", "require"):
            self._register_active_providers(
                critical=self._provider_checks == "require"
            )
        if self._depth == 1 and self._liveness is not None:
            self._start_liveness(self._liveness)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the health checks.

        Drops the checks `provider_checks` registered, so the next run checks the
        Providers that run opens rather than the ones this run had.
        """
        self._depth -= 1
        if self._depth > 0:
            return
        try:
            await self._stop_liveness()
        finally:
            for check_name in self._auto_registered:
                self._entries.pop(check_name, None)
            self._auto_registered.clear()

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
                "the aggregate. For a liveness check, only a critical "
                "failure counts toward ``failure_threshold``, turns "
                "``/livez`` to 503 and stops the worker. A non-critical "
                "one is logged like any failed check, and nothing more."
            ),
        ] = True,
        timeout: Annotated[
            PositiveFloat | None,
            Doc(
                "Per-check timeout override. Falls back to the "
                "default when omitted."
            ),
        ] = None,
        liveness: Annotated[
            bool,
            Doc(
                "Run this check for `/livez` instead of `/readyz` and "
                "`/healthz`. A liveness check looks inside the process only, "
                "such as whether a consumer made progress, never at a "
                "database. It runs every `Liveness.interval` seconds. "
                "Refused when `HealthChecks(liveness=...)` is not set."
            ),
        ] = False,
    ) -> None:
        """Register a health check function.

        Raises:
            ValueError: If ``name`` is already registered, or does not
                match ``^[a-z0-9][a-z0-9:_-]*$`` (max 64 chars), or
                ``liveness=True`` is set on a `HealthChecks` with no
                `Liveness`, or ``timeout`` is not a finite number of
                seconds greater than zero. A colon is allowed in
                ``name`` for namespacing, e.g.
                ``"weather:circuitbreaker"``.
        """
        config = self._config
        if not _valid_name(name):
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
        if timeout is not None:
            timeout = _checked_timeout(timeout)
        if liveness and self._liveness is None:
            msg = (
                f"Health check '{name}' sets liveness=True, but this "
                f"HealthChecks has no Liveness, so the check would never run. "
                f"Set HealthChecks(liveness=Liveness(...))."
            )
            raise ValueError(msg)
        self._entries[name] = _Entry(
            name=name,
            func=_normalize(func),
            critical=critical,
            timeout=timeout if timeout is not None else config.timeout,
            liveness=liveness,
        )
        mark_registered(func, Registered.HEALTH_CHECK, self)
        self._entries = dict(sorted(self._entries.items()))

    def check[FuncT: HealthCheckFunc](
        self,
        name: Annotated[str, Doc("Unique name identifying this check.")],
        *,
        critical: Annotated[
            bool,
            Doc(
                "Whether this check affects the aggregate status. For a "
                "liveness check, whether its failures can stop the worker."
            ),
        ] = True,
        timeout: Annotated[
            PositiveFloat | None,
            Doc("Per-check timeout override."),
        ] = None,
        liveness: Annotated[
            bool,
            Doc(
                "Run this check for `/livez` instead of `/readyz` and `/healthz`."
            ),
        ] = False,
    ) -> Callable[[FuncT], FuncT]:
        """Decorate an async function to register it as a health check.

        Registration is a side effect: the function is returned unchanged
        with its original signature, so ``await``-ing it directly (for
        example in a unit test) type-checks as usual.

        Example:
            >>> @health.check("database")
            ... async def check_db() -> dict | None:
            ...     return None
        """

        def decorator(func: FuncT) -> FuncT:
            self.add(
                name,
                func,
                critical=critical,
                timeout=timeout,
                liveness=liveness,
            )
            return func

        return decorator

    def add_provider(
        self,
        provider: Annotated[
            Provider,
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
            ValueError: If the provider ships no readiness check, the
                resulting name is already registered, or ``timeout`` is
                not a number of seconds greater than zero.
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

    def add_circuitbreaker(
        self,
        breaker: Annotated[
            CircuitBreaker,
            Doc(
                "The circuit breaker to report. A per-key circuit from "
                "``breaker.keyed(key)`` is reported on its own."
            ),
        ],
        *,
        name: Annotated[
            str | None,
            Doc(
                "Check name suffix. Defaults to the breaker's ``name``, "
                "so the check is ``circuitbreaker:inverter``. Pass an "
                "explicit name when the breaker name is not a valid "
                "check name."
            ),
        ] = None,
        critical: Annotated[
            bool,
            Doc(
                "Whether an open breaker fails ``/readyz``. Non-critical "
                "by default: an open breaker shows in the ``/healthz`` "
                "body and leaves readiness alone."
            ),
        ] = False,
        timeout: Annotated[
            PositiveFloat | None,
            Doc("Per-check timeout override. Falls back to the default."),
        ] = None,
    ) -> None:
        """Register a check on a circuit breaker as ``circuitbreaker:{name}``.

        Each run reads the breaker's state from its backend, so a
        replica that made no call through a shared breaker still reports
        it open. A closed breaker is ``ok``. A half-open or forced-closed
        breaker is ``ok`` with its state in the details. An open breaker
        fails with the seconds until its next probe, and a forced-open
        one fails until it is reset.

        Raises:
            ValueError: If the resulting name is not a valid check name
                (pass ``name=``), is already registered, or ``timeout``
                is not a number of seconds greater than zero.
        """
        from grelmicro.resilience.circuitbreaker import (  # noqa: PLC0415
            CircuitBreakerState,
        )

        check_name = f"circuitbreaker:{name or breaker.name}"
        if not _valid_name(check_name):
            msg = (
                f"Invalid health check name {check_name!r} for circuit "
                f"breaker {breaker.name!r}: must match "
                f"^[a-z0-9][a-z0-9:_-]*$ and be at most {_NAME_MAX_LEN} "
                f"chars. Pass name=... to set the part after "
                f"'circuitbreaker:'."
            )
            raise ValueError(msg)

        async def _check() -> HealthDetails | None:
            snapshot = await breaker._read_snapshot()  # noqa: SLF001
            state = snapshot.state
            if state == CircuitBreakerState.CLOSED:
                return None
            if state == CircuitBreakerState.OPEN:
                retry_after = ceil(snapshot.retry_after)
                msg = f"circuit breaker open, next probe in {retry_after}s"
                raise HealthError(
                    msg,
                    details={"state": str(state), "retry_after": retry_after},
                )
            if state == CircuitBreakerState.FORCED_OPEN:
                msg = "circuit breaker forced open until reset"
                raise HealthError(msg, details={"state": str(state)})
            return {"state": str(state)}

        self.add(check_name, _check, critical=critical, timeout=timeout)

    def _register_active_providers(self, *, critical: bool) -> None:
        """Register a check for every Provider active on the app.

        Called from `__aenter__` when `provider_checks` is `"report"` or
        `"require"`, with `critical` set for `"require"`. A provider with
        no readiness check is skipped. A provider already registered under
        any name (an explicit `add_provider`, or a second `__aenter__`) is
        left untouched, so the explicit registration wins and re-entry is
        idempotent. A `provider:{short_name}` name held by a different
        provider (two providers of the same vendor) is skipped with a
        warning, pointing at the explicit `add_provider(provider, name=...)`
        form. Opened outside an app, `"report"` registers nothing.

        Raises:
            NoActiveAppError: If `critical` is set and no app is active.
        """
        from grelmicro._app import Grelmicro, _current_micro  # noqa: PLC0415

        micro = Grelmicro.current() if critical else _current_micro.get(None)
        if micro is None:
            return
        for provider in micro.providers:
            if not self._discovers(provider):
                continue
            check_name = f"provider:{provider.short_name}"
            if check_name in self._entries:
                logger.warning(
                    "provider_checks: %r is already registered, skipping %r. "
                    "Register it with "
                    "health.add_provider(provider, name=...) to give it a "
                    "distinct name.",
                    check_name,
                    provider,
                )
                continue
            self.add(check_name, provider.check, critical=critical)
            self._auto_registered.append(check_name)

    def _discovers(self, item: object) -> bool:
        """Return whether `provider_checks=` would register a check for `item`.

        True for a Provider with a readiness check that no check here
        already runs.
        """
        if not isinstance(item, Provider) or type(item).check is Provider.check:
            return False
        check = item.check
        return not any(entry.func == check for entry in self._entries.values())

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
        check for ``cache_ttl``. Concurrent calls for the
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
            if name not in excluded
            and not entry.liveness
            and (not critical_only or entry.critical)
            and not _left_closed_by_fake(entry.func)
        ]

        if not selected:
            return HealthReport(status=HealthStatus.OK, checks={})

        results: dict[str, CheckResult] = {}
        cache_ttl = nanoseconds(config.cache_ttl)

        async def _run(name: str, entry: _Entry) -> None:
            results[name] = await self._get_or_run(entry, cache_ttl=cache_ttl)

        async with asyncio.TaskGroup() as tg:
            for name, entry in selected:
                tg.create_task(_run(name, entry))

        ordered = {name: results[name] for name, _ in selected}
        return HealthReport(
            status=self._aggregate_status(ordered.values()),
            checks=ordered,
        )

    def _start_liveness(self, liveness: Liveness) -> None:
        """Start the liveness task, which also arms the loop watchdog."""
        self._liveness_failures = 0
        self._liveness_task = asyncio.create_task(
            self._run_liveness(liveness), name="grelmicro-liveness"
        )
        self._liveness_task.add_done_callback(
            lambda task: self._liveness_ended(task, liveness)
        )

    def _liveness_ended(
        self, task: asyncio.Task[None], liveness: Liveness
    ) -> None:
        """Stop the worker when an error ended the running liveness task.

        The error is logged once. An error from the task of a run that
        already closed is logged and stops nothing.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        if task is not self._liveness_task:
            logger.error(
                "Liveness checks of a closed run ended on an unexpected error.",
                exc_info=exc,
            )
            return
        logger.critical(
            "Liveness checks stopped on an unexpected error. Stopping the "
            "worker so it is replaced.",
            exc_info=exc,
        )
        _liveness._stop_process()  # noqa: SLF001
        _liveness._exit_after(liveness.shutdown_timeout)  # noqa: SLF001

    async def _stop_liveness(self) -> None:
        """Stop the liveness task and the watchdog."""
        task, self._liveness_task = self._liveness_task, None
        try:
            if task is not None:
                task.cancel()
                await asyncio.wait({task})
        finally:
            watchdog, self._watchdog = self._watchdog, None
            if watchdog is not None:
                await asyncio.to_thread(watchdog.stop)

    async def _run_liveness(self, liveness: Liveness) -> None:
        """Arm the watchdog, then run the liveness checks.

        The watchdog and the liveness rounds pause while the app is not open,
        so a component that blocks the loop while it opens or closes is not
        taken for a stuck worker. They resume when the app reopens.
        """
        app_open = _app_open()
        if liveness.stall_timeout is not None:
            self._watchdog = Watchdog(
                asyncio.get_running_loop(), liveness.stall_timeout, app_open
            )
            self._watchdog.start()
        while True:
            await asyncio.sleep(liveness.interval)
            entries = [e for e in self._entries.values() if e.liveness]
            if not entries or not app_open():
                continue
            results = await asyncio.gather(*(_run_check(e) for e in entries))
            failed = [
                entry.name
                for entry, result in zip(entries, results, strict=True)
                if result["status"] == HealthStatus.ERROR and entry.critical
            ]
            if not failed:
                self._liveness_failures = 0
                continue
            self._liveness_failures += 1
            if self._liveness_failures < liveness.failure_threshold:
                continue
            logger.critical(
                "Liveness check failed %d times in a row: %s. Stopping the "
                "worker so it is replaced.",
                self._liveness_failures,
                ", ".join(failed),
            )
            _liveness._stop_process()  # noqa: SLF001
            _liveness._exit_after(liveness.shutdown_timeout)  # noqa: SLF001
            return

    async def _get_or_run(
        self, entry: _Entry, *, cache_ttl: int
    ) -> CheckResult:
        """Return a cached or freshly computed result for one check.

        Serves a cached result until it is ``cache_ttl`` nanoseconds old,
        and serializes concurrent calls via a
        shared ``asyncio.Event``. If the single-flight leader is
        cancelled before it produces a result, waiters take the lead
        themselves instead of failing. The caller captures
        ``cache_ttl`` from a snapshot at the start of the round so a
        concurrent ``reconfigure`` cannot change the cache decision
        mid-call.
        """
        ttl = cache_ttl
        while True:
            now = monotonic_ns()
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
                entry.cached_at = monotonic_ns()
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
    start = monotonic()
    result = await _run_check_inner(entry)
    elapsed = monotonic() - start
    healthy = result["status"] == HealthStatus.OK
    _emit.observe(
        "grelmicro.health.check.up",
        1 if healthy else 0,
        {
            "grelmicro.health.check.name": entry.name,
            "grelmicro.health.check.critical": entry.critical,
        },
    )
    _emit.record_duration(
        "grelmicro.health.check.duration",
        elapsed,
        {
            "grelmicro.health.check.name": entry.name,
            "grelmicro.outcome": "success" if healthy else "error",
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
