"""Configuration helpers for grelmicro components.

Exposes:

- `resolve_config`: build a validated Pydantic config from a pre-built
  instance or from kwargs merged with environment variables.
- `env_segment`: normalise an instance name into a POSIX env var
  segment.
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
from functools import lru_cache
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar
from weakref import WeakSet

from pydantic import BaseModel, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from grelmicro._json import json_loads

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Mapping

    from grelmicro.errors import SettingsValidationError

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


_ENV_LOAD_VAR = "GREL_ENV_LOAD"
_ENV_LOAD_TRUTHY = frozenset({"1", "true", "yes", "on"})


def env_load_default() -> bool:
    """Return True when env-driven configuration is opted in process-wide.

    Reads ``GREL_ENV_LOAD`` and accepts ``1``, ``true``, ``yes``,
    ``on`` (case-insensitive) as truthy.
    """
    return os.environ.get(_ENV_LOAD_VAR, "").strip().lower() in _ENV_LOAD_TRUTHY


def resolve_config[C: BaseModel](
    config_cls: type[C],
    *,
    explicit: C | None,
    kwargs: Mapping[str, object | None],
    env_prefix: str,
    env_load: bool | None = None,
    error_type: type[SettingsValidationError] | None = None,
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

    if env_load is None:
        env_load = env_load_default()

    try:
        if not env_load:
            return config_cls.model_validate(provided)

        settings_cls = _build_settings_cls(config_cls, env_prefix)
        # The dynamic subclass is built at runtime via `type(...)` below, so
        # neither mypy nor ty can prove that `settings_cls` accepts the kwargs
        # declared on `config_cls` or that it returns `C`. Pydantic's runtime
        # validation enforces the contract.
        return settings_cls(**provided)  # type: ignore[return-value, arg-type]  # ty: ignore[invalid-return-type, invalid-argument-type]
    except ValidationError as error:
        if error_type is None:
            raise
        raise error_type(error) from None


@lru_cache(maxsize=256)
def _build_settings_cls[C: BaseModel](
    config_cls: type[C],
    env_prefix: str,
) -> type[BaseSettings]:
    """Create a one-off BaseSettings subclass that reads env vars.

    The dynamic class inherits ``config_cls`` so all fields,
    validators, and ``model_config`` flags (``frozen``, ``extra``)
    are preserved. Only the ``env_prefix`` is added.

    Cached on ``(config_cls, env_prefix)`` with a bounded LRU. The
    expected keyspace is small (one entry per declared component
    instance per process); the bound is a safety net for long-
    running processes that derive prefixes from runtime inputs.
    """
    # `model_config` is a TypedDict (`SettingsConfigDict`/`ConfigDict`).
    # Spreading it into a plain dict to add `env_prefix` widens the value
    # type to `dict[str, object]`, which downstream constructors accept
    # at runtime but mypy/ty cannot narrow back to the TypedDict shape.
    merged_config: dict[str, object] = {**(config_cls.model_config or {})}  # type: ignore[dict-item]
    merged_config["env_prefix"] = env_prefix
    # `SettingsConfigDict(**merged_config)` round-trips a `dict[str, object]`
    # through a TypedDict constructor. Static checkers reject the widened
    # value types even though Pydantic's runtime validator accepts them.
    return type(
        f"_{config_cls.__name__}Settings",
        (config_cls, BaseSettings),
        {"model_config": SettingsConfigDict(**merged_config)},  # type: ignore[typeddict-item]
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
        if type(new_config) is not type(current):
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
