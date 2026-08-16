"""Configuration helpers for grelmicro components.

Exposes:

- `resolve_config`: build a validated Pydantic config from a pre-built
  instance or from kwargs merged with environment variables.
- `env_segment`: normalise an instance name into a POSIX env var
  segment.
- `default_env_prefix`: build a component instance's env prefix,
  dropping the name segment for the default instance.
- `parse_csv_or_json`: coerce an env var string into a list, accepting
  comma-separated or JSON-array form.
- `Reconfigurable`: mixin providing atomic live reconfiguration for
  stateful components.

The full contract, including the precedence rules and the
name-as-namespace convention, is documented in
`docs/architecture/config.md`.
"""

from __future__ import annotations

import logging
import os
import re
import warnings
from collections import deque

# Imported at runtime, not under `TYPE_CHECKING`: both appear in the
# annotations of `resolve_config` and `defer_report`, which
# `typing.get_type_hints` has to resolve from module globals.
from collections.abc import Callable, Mapping  # noqa: TC003
from copy import copy
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, TypeVar
from weakref import WeakSet

from pydantic import AliasChoices, BaseModel, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from grelmicro._diagnostics import ENV_LOAD_OFF, diagnostic
from grelmicro._json import json_loads
from grelmicro.errors import EnvLoadOffWarning, SettingsValidationError

if TYPE_CHECKING:
    import asyncio

C = TypeVar("C", bound=BaseModel)
ConfigT = TypeVar("ConfigT", bound=BaseModel)

logger = logging.getLogger("grelmicro")

_NON_ENV_CHARS = re.compile(r"[^A-Z0-9_]+")
_REPEATED_UNDERSCORES = re.compile(r"_+")


def parse_csv_or_json(value: Any) -> Any:  # noqa: ANN401
    """Coerce a string into a list, accepting CSV or JSON-array form.

    Pass-through for any non-string value. Strings starting with `[`
    are parsed as JSON arrays. Otherwise the string is split on commas
    and each item is stripped. Empty items are dropped.
    """
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("["):
            return json_loads(s)
        return [item.strip() for item in s.split(",") if item.strip()]
    return value


def env_segment(name: str) -> str:
    """Normalise an instance ``name`` into a POSIX env var segment.

    Returns the upper-cased name with every character outside
    ``[A-Z0-9_]`` replaced by ``_`` and any run of underscores
    collapsed to a single underscore. Leading and trailing
    underscores are stripped. The result is suitable as a
    component of an environment variable name on every POSIX
    shell.

    Examples:
        ``cart`` -> ``CART``
        ``payments-eu`` -> ``PAYMENTS_EU``
        ``cart.v2`` -> ``CART_V2``
        ``foo:bar`` -> ``FOO_BAR``
        ``weather/svc`` -> ``WEATHER_SVC``
        ``my--lock`` -> ``MY_LOCK``

    Raises ``ValueError`` if the input produces an empty result
    (every character was non-portable) or starts with a digit
    (env var names must start with a letter or underscore).
    """
    upper = name.upper()
    cleaned = _NON_ENV_CHARS.sub("_", upper)
    cleaned = _REPEATED_UNDERSCORES.sub("_", cleaned).strip("_")
    if not cleaned:
        msg = (
            f"name {name!r} produces an empty environment variable "
            f"segment. Pick a name with at least one letter or digit."
        )
        raise ValueError(msg)
    if cleaned[0].isdigit():
        msg = (
            f"name {name!r} produces env segment {cleaned!r} that "
            f"starts with a digit. Env var names must start with a "
            f"letter or underscore."
        )
        raise ValueError(msg)
    return cleaned


def default_env_prefix(component: str, name: str) -> str:
    """Build the env prefix for a component instance.

    The default instance drops the name segment, so its env vars read
    `GREL_{COMPONENT}_{FIELD}`. A named instance keeps the segment:
    `GREL_{COMPONENT}_{NAME}_{FIELD}`.
    """
    if name == "default":
        return f"GREL_{component}_"
    return f"GREL_{component}_{env_segment(name)}_"


def kind_env_prefix(component: str) -> str:
    """Build the kind-wide env prefix, `GREL_{COMPONENT}_`.

    Every instance of the component falls back to these variables when its
    own are unset, so one variable retunes a whole kind. A service with
    twelve named locks sets `GREL_LOCK_LEASE_DURATION` once instead of
    twelve times.
    """
    return f"GREL_{component}_"


def env_prefixes(
    component: str,
    name: str,
    override: str | None = None,
) -> tuple[str, str | None]:
    """Return the instance prefix and the kind prefix it falls back to.

    The kind prefix is `None` when there is nothing to fall back to: either
    the caller supplied an explicit `override`, which means "read exactly
    these variables", or the instance is the default one, which already owns
    the bare prefix.
    """
    if override:
        return override, None
    instance = default_env_prefix(component, name)
    kind = kind_env_prefix(component)
    return instance, (None if instance == kind else kind)


_ENV_LOAD_VAR = "GREL_ENV_LOAD"
_ENV_LOAD_TRUTHY = frozenset({"1", "true", "yes", "on"})


def env_load_default() -> bool:
    """Return True when env-driven configuration is opted in process-wide.

    Reads ``GREL_ENV_LOAD`` and accepts ``1``, ``true``, ``yes``,
    ``on`` (case-insensitive) as truthy.
    """
    return os.environ.get(_ENV_LOAD_VAR, "").strip().lower() in _ENV_LOAD_TRUTHY


def _union_arms(union: object) -> tuple[type[BaseModel], ...]:
    """Return the config classes a discriminated union is made of.

    Unwraps `Annotated[A | B, Discriminator("kind")]` down to `(A, B)`.
    A non-union returns empty, so a caller can pass one unconditionally.
    """
    from typing import get_args, get_origin  # noqa: PLC0415

    inner = union
    if get_origin(inner) is Annotated:
        inner = get_args(inner)[0]
    arms = get_args(inner)
    return tuple(
        arm
        for arm in arms
        if isinstance(arm, type) and issubclass(arm, BaseModel)
    )


def _sibling_fields(
    union: object | None, config_cls: type[BaseModel]
) -> frozenset[str]:
    """Return field names other arms declare and `config_cls` does not.

    These are the names an operator reaches for when they believe a
    different algorithm is running. They belong to the pattern, so a
    variable naming one is ours to report, unlike an arbitrary name under
    the same prefix.
    """
    if union is None:
        return frozenset()
    mine = set(config_cls.model_fields)
    others: set[str] = set()
    for arm in _union_arms(union):
        if arm is not config_cls:
            others |= set(arm.model_fields)
    return frozenset(others - mine)


def _running_kind(config_cls: type[BaseModel]) -> str:
    """Return the algorithm name a config class carries in its `kind` field."""
    field = config_cls.model_fields.get("kind")
    default = getattr(field, "default", None)
    return str(default) if default is not None else config_cls.__name__


def _reject_cross_arm_env(
    config_cls: type[BaseModel],
    union: object | None,
    env_prefix: str,
    kind_env_prefix: str | None,
    error_type: type[SettingsValidationError] | None,
) -> None:
    """Refuse to start when one instance is handed another algorithm's variable.

    Only the instance address is checked. The bare kind prefix is a
    broadcast: a fleet running both algorithms legitimately tunes its
    token buckets with `GREL_RATELIMITER_CAPACITY` while sliding-window
    limiters ignore it, so warning there would fire on every start.

    An instance address names one object whose algorithm is known, so the
    variable is an unambiguous mistake. Running on would let a deployment
    believe a limit is enforced when it is not, which is the silent drift
    this contract exists to prevent.

    Raises:
        SettingsValidationError: If a variable at the instance address
            names a field belonging to another algorithm.
    """
    if kind_env_prefix is None:
        return
    sibling = _sibling_fields(union, config_cls)
    found = sorted(
        f"{env_prefix}{field.upper()}"
        for field in sibling
        if f"{env_prefix}{field.upper()}" in os.environ
    )
    if not found:
        return
    kind = _running_kind(config_cls)
    names = ", ".join(found)
    msg = (
        f"{names} names a field of a different algorithm, but this "
        f"instance runs {kind!r}. The environment tunes an algorithm's "
        f"fields and never selects the algorithm. Remove the variable or "
        f"build the instance with the algorithm it belongs to."
    )
    raise (error_type or SettingsValidationError)(msg)


def resolve_config[C: BaseModel](
    config_cls: type[C],
    *,
    explicit: C | None,
    kwargs: Mapping[str, object | None],
    env_prefix: str,
    env_load: bool | None = None,
    shared_env: Mapping[str, str] | None = None,
    kind_env_prefix: str | None = None,
    error_type: type[SettingsValidationError] | None = None,
    union: object | None = None,
) -> C:
    """Build a validated ``config_cls`` from an explicit instance or kwargs and env.

    Resolution has two mutually exclusive modes. If ``explicit`` is
    provided, it is returned as-is and any non-``None`` value in
    ``kwargs`` raises ``TypeError``. Otherwise, ``None`` kwarg values
    are treated as unset and never reach the model, caller-supplied
    non-``None`` kwargs win over environment variables, and
    environment variables win over defaults declared on ``config_cls``.

    The env path is opt-in. When ``env_load`` is ``None`` (the
    default), the process-wide ``GREL_ENV_LOAD`` flag decides:
    env reads run only when it is set to a truthy value. Pass
    ``env_load=True`` or ``env_load=False`` on the call to override
    the flag for that construction.

    The env path constructs a one-off ``BaseSettings`` subclass that
    inherits ``config_cls`` so its validators, ``frozen``, and
    ``extra`` flags are preserved. Only the ``env_prefix`` is added.

    ``shared_env`` maps a field name to an app-wide variable that fills
    it when the component's own variable is unset, as
    ``{"timezone": "GREL_TIMEZONE"}`` does. The component variable
    always wins, and a keyword argument still wins over both. See
    `docs/architecture/config.md` for what qualifies as app-wide.

    ``kind_env_prefix`` names the kind-wide prefix a named instance falls
    back to, so ``GREL_LOCK_LEASE_DURATION`` retunes every ``Lock`` that
    does not set its own ``GREL_LOCK_{NAME}_LEASE_DURATION``. Build it with
    ``env_prefixes``, which returns ``None`` for the default instance and
    for a caller-supplied prefix.

    Pass ``error_type`` to wrap a ``pydantic.ValidationError`` into a
    component-specific ``SettingsValidationError``. Without it, the
    raw ``pydantic.ValidationError`` propagates.

    See `docs/architecture/config.md` for the full contract,
    including the name-as-namespace convention used to derive
    component-specific prefixes.
    """
    provided = {k: v for k, v in kwargs.items() if v is not None}

    if explicit is not None:
        if provided:
            msg = "pass a pre-built config OR individual kwargs, not both"
            raise TypeError(msg)
        return explicit

    # An explicit `env_load=False` is a deliberate opt-out and is never
    # reported. Only the process-wide flag being unset is worth a warning,
    # since that is the state a caller reaches without choosing it.
    implicit = env_load is None
    if implicit:
        env_load = env_load_default()

    sibling = _sibling_fields(union, config_cls)

    try:
        if not env_load:
            if implicit:
                _warn_ignored_env(
                    config_cls,
                    env_prefix,
                    provided,
                    shared_env,
                    sibling,
                    kind_env_prefix,
                )
            return config_cls.model_validate(provided)

        _reject_cross_arm_env(
            config_cls, union, env_prefix, kind_env_prefix, error_type
        )
        settings_cls = _build_settings_cls(
            config_cls,
            env_prefix,
            tuple(sorted(shared_env.items())) if shared_env else (),
            kind_env_prefix,
        )
        # The dynamic subclass is built at runtime via `type(...)` below, so
        # ty cannot prove that `settings_cls` accepts the kwargs declared on
        # `config_cls` or that it returns `C`. Pydantic's runtime validation
        # enforces the contract.
        return settings_cls(**provided)  # ty: ignore[invalid-return-type, invalid-argument-type]
    except ValidationError as error:
        if error_type is None:
            raise
        raise error_type(error) from None


_warned_ignored_env: set[str] = set()
"""Variable names already reported by `_warn_ignored_env`, process-wide.

A component is often constructed many times, and the same misconfiguration
would otherwise be reported on every construction.
"""

_pending_ignored_env: deque[str] = deque()
"""Reported variable names still waiting for a log record.

A component resolves its config before logging is configured, so the names
queue here until `flush_ignored_env_reports` drains them.
"""

_pending_reports: deque[Callable[[], None]] = deque()
"""Startup reports from elsewhere in grelmicro, waiting for logging.

Same queue discipline as `_pending_ignored_env`, for a report that carries
more than a variable name. `_environment` puts the backend scope report here.
"""

_logging_configured = False
"""True while `grelmicro.log` has its handlers installed."""

_IGNORED_ENV_MESSAGE = (
    "%s is set but was not applied: environment-driven configuration is "
    "opt-in. Set %s=1 to enable it, or pass the value directly."
)
"""Report text, shared by the `warnings` and the `logging` channel."""


def _warn_ignored_env(
    config_cls: type[BaseModel],
    env_prefix: str,
    provided: Mapping[str, object],
    shared_env: Mapping[str, str] | None = None,
    sibling: frozenset[str] = frozenset(),
    kind_env_prefix: str | None = None,
) -> None:
    """Report a variable this config declares that is set but will not be read.

    Only the exact names `config_cls` declares are looked up, plus the
    app-wide name each `shared_env` field falls back to. A prefix scan
    would match unrelated variables, and Kubernetes injects
    `{SVCNAME}_SERVICE_HOST` for every Service, so a Service named `grel-log`
    would produce a warning on every pod start.

    A field the caller passed is skipped: a keyword argument outranks the
    environment, so that variable would not have applied either way and the
    caller has already done what the message would ask of them.

    Each name is reported on both channels. `warnings` carries it under
    `GrelmicroConfigWarning`, which a test suite can filter or turn into an
    error. `logging` carries it on the `grelmicro` logger with the name in a
    `variable` field, so a pipeline can match the field instead of the
    message text. The log record waits until logging is configured.
    """
    for field in (*config_cls.model_fields, *sorted(sibling)):
        if field in provided:
            continue
        names = [f"{env_prefix}{field.upper()}"]
        # The kind address applies to every instance since 0.38.0, so a
        # variable set there would have been read too. Still an exact-name
        # lookup of a declared field, never a prefix sweep.
        if kind_env_prefix:
            names.append(f"{kind_env_prefix}{field.upper()}")
        if shared_env and field in shared_env:
            names.append(shared_env[field])
        for name in names:
            if name not in os.environ or name in _warned_ignored_env:
                continue
            _warned_ignored_env.add(name)
            warnings.warn(
                diagnostic(
                    ENV_LOAD_OFF, _IGNORED_ENV_MESSAGE % (name, _ENV_LOAD_VAR)
                ),
                EnvLoadOffWarning,
                stacklevel=4,
            )
            if _logging_configured:
                _log_ignored_env(name)
            else:
                _pending_ignored_env.append(name)


def _log_ignored_env(name: str) -> None:
    """Emit one ignored-variable report on the `grelmicro` logger.

    Rendered before it reaches `logging`, so this channel carries the same
    sentence and the same diagnostic code as the `warnings` one, and the
    record holds no positional arguments a formatter could read as something
    else. The code also travels as a structured field.
    """
    logger.warning(
        diagnostic(ENV_LOAD_OFF, _IGNORED_ENV_MESSAGE % (name, _ENV_LOAD_VAR)),
        extra={"variable": name, "diagnostic": ENV_LOAD_OFF},
    )


def flush_ignored_env_reports() -> None:
    """Emit the queued ignored-variable reports as log records.

    Called by `grelmicro.log` once the backend has installed its handlers, so
    a queued report is emitted with logging in place. Later reports go
    straight to the logger.
    """
    global _logging_configured  # noqa: PLW0603
    _logging_configured = True
    while _pending_ignored_env:
        _log_ignored_env(_pending_ignored_env.popleft())
    while _pending_reports:
        _pending_reports.popleft()()


def defer_report(emit: Callable[[], None]) -> None:
    """Emit a startup log record now, or queue it until logging is configured.

    The `warnings` channel fires where the report is made. The log record
    waits, so a report made before `Log` installs its handlers still lands in
    the log stream instead of a root logger with nothing on it.
    """
    if _logging_configured:
        emit()
    else:
        _pending_reports.append(emit)


def hold_ignored_env_reports() -> None:
    """Queue the reports again, until logging is configured once more.

    Called by `grelmicro.log` when the `Log` component restores a root logger
    that had no logging on it. A report made between two lifecycles then waits
    for the next one instead of reaching a root logger with nothing installed.
    """
    global _logging_configured  # noqa: PLW0603
    _logging_configured = False


def ignored_env_reports_enabled() -> bool:
    """Return True while a report goes straight to the logger."""
    return _logging_configured


@lru_cache(maxsize=256)
def _build_settings_cls[C: BaseModel](
    config_cls: type[C],
    env_prefix: str,
    shared_env: tuple[tuple[str, str], ...] = (),
    kind_env_prefix: str | None = None,
) -> type[BaseSettings]:
    """Create a one-off BaseSettings subclass that reads env vars.

    The dynamic class inherits ``config_cls`` so all fields,
    validators, and ``model_config`` flags (``frozen``, ``extra``)
    are preserved. Only the ``env_prefix`` is added.

    Each ``shared_env`` field is redeclared with an ``AliasChoices``
    naming the component's own variable first and the app-wide one
    second, so the component variable wins and pydantic-settings keeps
    doing the matching. The prefix is composed into the alias rather
    than assumed, so a caller-supplied ``env_prefix`` still applies.
    Redeclaring a field replaces its name for population, hence
    ``populate_by_name`` so keyword arguments keep working under
    ``extra="forbid"``.

    Cached on ``(config_cls, env_prefix, shared_env)`` with a bounded
    LRU. The expected keyspace is small (one entry per declared
    component instance per process); the bound is a safety net for long-
    running processes that derive prefixes from runtime inputs.
    """
    # `model_config` is a TypedDict (`SettingsConfigDict`/`ConfigDict`).
    # Spreading it into a plain dict to add `env_prefix` widens the value
    # type to `dict[str, object]`.
    merged_config: dict[str, object] = {**(config_cls.model_config or {})}
    merged_config["env_prefix"] = env_prefix
    namespace: dict[str, Any] = {}
    shared_map = dict(shared_env)
    if shared_map or kind_env_prefix:
        # A redeclared field is populated by its alias only, so the field
        # name itself would read as an extra input and the keyword path
        # would stop working under `extra="forbid"`. `populate_by_name`
        # is the spelling that works on the whole supported pydantic
        # range; `validate_by_name` needs 2.11.
        merged_config["populate_by_name"] = True
        annotations: dict[str, Any] = {}
        for field, info in config_cls.model_fields.items():
            # Most specific first: this instance's own variable, then the
            # kind-wide one every instance shares, then the app-wide one.
            # pydantic-settings takes the first that is set.
            choices = [f"{env_prefix}{field.upper()}"]
            if kind_env_prefix:
                choices.append(f"{kind_env_prefix}{field.upper()}")
            shared_var = shared_map.get(field)
            if shared_var:
                choices.append(shared_var)
            if len(choices) == 1:
                continue
            # Copy the original `FieldInfo` rather than building a new one,
            # so a default factory, a description, or any other attribute
            # carries over. The annotation is taken bare, because the
            # copied `FieldInfo` already holds the metadata and passing
            # both would embed it twice.
            aliased = copy(info)
            aliased.validation_alias = AliasChoices(*choices)
            annotations[field] = info.annotation
            namespace[field] = aliased
        namespace["__annotations__"] = annotations
    namespace["model_config"] = SettingsConfigDict(**merged_config)
    return type(
        f"_{config_cls.__name__}Settings",
        (config_cls, BaseSettings),
        namespace,
    )


class Reconfigurable[ConfigT: BaseModel]:
    """Mixin that adds atomic live reconfiguration to a component.

    Subclasses initialize `self._config` and
    `self._reconfigure_lock = asyncio.Lock()` in `__init__`, and
    override `_apply_reconfigure` to rebuild any cached derived
    state. The default `_apply_reconfigure` is a no-op.

    See [Live reconfiguration](../architecture/reconfigure.md) for
    the full contract.
    """

    _config: ConfigT
    _reconfigure_lock: asyncio.Lock
    _env_prefix: str | None = None

    _IMMUTABLE_RECONFIGURE_FIELDS: ClassVar[frozenset[str]] = frozenset()
    """Field names a live reconfigure must never patch from external config.

    `resolve_config_from_mapping` skips any key whose suffix names one of
    these fields, so a co-located mutable change in the same mapping still
    applies instead of being dropped when the whole instance is rejected.
    """

    @property
    def config(self) -> ConfigT:
        """Return the current configuration."""
        return self._config

    def _track_reconfigure(self, env_prefix: str) -> None:
        """Record the env prefix and register for external reload.

        Called from a component's constructor under its derived
        name-as-namespace `env_prefix`. The recorded prefix lets
        `ExternalConfig` re-resolve this instance from a mounted
        ConfigMap or Secret using the same keys the environment uses,
        whether or not the instance loaded any value from the
        environment at construction. Instances built from a pre-built
        config (the declarative `from_config` path) skip this and stay
        static.
        """
        self._env_prefix = env_prefix
        _reconfigurables.add(self)

    async def reconfigure(self, new_config: ConfigT) -> None:
        """Atomically swap to `new_config`.

        Operations in flight when `reconfigure` is called complete on
        the previous config. Operations started after `reconfigure`
        returns see the new config. Equal configs are a no-op.

        Raises:
            TypeError: If `new_config` is not the same runtime type
                as the current config.
        """
        current = self._config
        # The env path builds a `BaseSettings` subclass of the declared
        # config, so an instance constructed from the environment holds a
        # `_LockConfigSettings` where the caller has a `LockConfig`. They
        # are the same configuration, so either direction of the subclass
        # relation is accepted. Two arms of one algorithm union are
        # siblings, neither a subclass of the other, so they are still
        # rejected.
        if not isinstance(new_config, type(current)) and not isinstance(
            current, type(new_config)
        ):
            msg = (
                f"reconfigure requires {type(current).__name__}, "
                f"got {type(new_config).__name__}"
            )
            raise TypeError(msg)
        if new_config == current:
            return
        async with self._reconfigure_lock:
            # Double-checked locking. A concurrent caller can win the lock
            # first and install the same `new_config`; this re-read avoids
            # rebinding twice. Not deterministically reachable from a
            # single-event-loop test, so coverage is excluded by design.
            if new_config == self._config:  # pragma: no cover
                return
            await self._apply_reconfigure(new_config)
            self._config = new_config

    async def _apply_reconfigure(self, new_config: ConfigT) -> None:
        """Rebuild cached derived state for `new_config`.

        Runs under `self._reconfigure_lock`. Must not assign
        `self._config`. The default does nothing.
        """


_reconfigurables: WeakSet[Reconfigurable[Any]] = WeakSet()
"""Live `Reconfigurable` instances registered under a name-as-namespace prefix.

Process-global and weakly held: an instance drops out when it is garbage
collected, so a module-level `Lock("ledger")` is tracked for as long as it
lives without pinning it. `ExternalConfig` reads this set to reconfigure
every live instance from a mounted ConfigMap or Secret.
"""


def reconfigurable_instances() -> list[Reconfigurable[Any]]:
    """Return the live `Reconfigurable` instances registered for reload."""
    return list(_reconfigurables)


def resolve_config_from_mapping[C: BaseModel](
    current: C,
    *,
    env_prefix: str,
    mapping: Mapping[str, str],
    immutable_fields: frozenset[str] = frozenset(),
    error_type: type[SettingsValidationError] | None = None,
) -> C:
    """Patch `current` with values from a flat env-style `mapping`.

    Keys are matched case-insensitively against `env_prefix`. Only keys
    whose suffix names a field on the config are applied, so unrelated
    keys in a shared ConfigMap are ignored and every field the mapping
    omits keeps its current value. Keys naming an `immutable_fields`
    entry (a lock `worker`) are skipped, so a co-located mutable change
    in the same mapping still applies instead of being dropped because
    the immutable field cannot change.

    Present values are coerced through the model's own validators, so a
    CSV or JSON-array string resolves into a list exactly as it does
    from the environment. Returns `current` unchanged when the mapping
    carries nothing for this prefix.

    Pass `error_type` to wrap a `pydantic.ValidationError` into a
    component-specific `SettingsValidationError`. Without it, the raw
    `pydantic.ValidationError` propagates.

    Raises:
        pydantic.ValidationError: If a present value fails validation
            and no `error_type` is given.
    """
    cls = type(current)
    fields = cls.model_fields
    prefix_len = len(env_prefix)
    prefix_upper = env_prefix.upper()
    overrides: dict[str, str] = {}
    unmatched = 0
    for key, value in mapping.items():
        if not key.upper().startswith(prefix_upper):
            continue
        field = key[prefix_len:].lower()
        if field in immutable_fields:
            _warn_immutable_skipped(current, env_prefix, field, value)
            continue
        if field in fields:
            overrides[field] = value
        else:
            unmatched += 1
    if unmatched:
        # Key names are not logged: in a directory-mounted Secret the
        # filename is the key, so a name itself can be sensitive.
        logger.debug(
            "External config carries %d key(s) under %s that match no "
            "field on %s",
            unmatched,
            env_prefix,
            cls.__name__,
        )
    if not overrides:
        return current
    try:
        return cls.model_validate({**current.model_dump(), **overrides})
    except ValidationError as error:
        if error_type is None:
            raise
        raise error_type(error) from None


_warned_immutable_skipped: set[str] = set()
"""Immutable-field keys already reported by `_warn_immutable_skipped`.

The same mounted source is re-read on every resync, so a key that stays
put would otherwise be reported for as long as the process runs.
"""


def _warn_immutable_skipped(
    current: BaseModel,
    env_prefix: str,
    field: str,
    value: str,
) -> None:
    """Report an attempt to live-change a field that only applies at startup.

    Only a value that differs from the running one is reported. The usual
    deployment mounts the same source that seeded the environment at
    startup, so every startup-only key in it arrives on each resync
    carrying the value already in effect. Reporting those would drown the
    one case worth seeing, which is an operator editing a field that
    cannot take effect until the next restart.

    The value is never logged: a mounted Secret can carry one under any
    field name.
    """
    cls = type(current)
    if field not in cls.model_fields:
        return
    try:
        candidate = cls.model_validate({**current.model_dump(), field: value})
    except ValidationError:
        # A value the model rejects cannot be the one already running, so
        # it is an attempted change and worth reporting.
        pass
    else:
        if getattr(candidate, field) == getattr(current, field):
            return
    marker = f"{env_prefix}{field.upper()}"
    if marker in _warned_immutable_skipped:
        return
    _warned_immutable_skipped.add(marker)
    logger.warning(
        "External config changes %s, which only applies at startup. "
        "The running value is kept until the process restarts.",
        marker,
        extra={"variable": marker},
    )


def _redact_validation_error(exc: ValidationError) -> str:
    """Summarize a `ValidationError` without ever echoing input values.

    Returns one `field: error_type` entry per error, joined with commas.
    The offending input is never included, so a Secret value patched in
    from a mounted source cannot leak into the logs.
    """
    parts = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(loc) for loc in error["loc"]) or "(root)"
        parts.append(f"{location}: {error['type']}")
    return ", ".join(parts)


async def reconfigure_all(mapping: Mapping[str, str]) -> None:
    """Reconfigure every live registered instance from `mapping`.

    Patches each instance from the flat env-style `mapping` and applies it
    through `reconfigure`, which is a no-op when the config is unchanged. A
    value the instance rejects (an invalid value, or an attempt to change an
    immutable field) is logged and skipped so one bad key never stops the
    others from updating.

    Validation failures log only field locations and error types, never the
    offending value, so a secret patched from a mounted source cannot leak.
    """
    for instance in list(_reconfigurables):
        env_prefix = instance._env_prefix  # noqa: SLF001
        if env_prefix is None:  # pragma: no cover
            continue
        try:
            new_config = resolve_config_from_mapping(
                instance._config,  # noqa: SLF001
                env_prefix=env_prefix,
                mapping=mapping,
                immutable_fields=instance._IMMUTABLE_RECONFIGURE_FIELDS,  # noqa: SLF001
            )
        except ValidationError as exc:
            logger.warning(
                "Ignoring invalid external config for %s: %s",
                env_prefix,
                _redact_validation_error(exc),
            )
            continue
        try:
            await instance.reconfigure(new_config)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "External config rejected for %s: %s", env_prefix, exc
            )
