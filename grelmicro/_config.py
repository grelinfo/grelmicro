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

import os
import re
from functools import lru_cache
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from grelmicro._json import json_loads

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Mapping

C = TypeVar("C", bound=BaseModel)
ConfigT = TypeVar("ConfigT", bound=BaseModel)

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
    Validation failures surface as ``pydantic.ValidationError``.

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

    if not env_load:
        return config_cls.model_validate(provided)

    settings_cls = _build_settings_cls(config_cls, env_prefix)
    return settings_cls(**provided)  # type: ignore[return-value, arg-type]  # ty: ignore[invalid-return-type, invalid-argument-type]


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
    # `model_config` is a TypedDict (`SettingsConfigDict`/`ConfigDict`);
    # spreading it into a plain dict so we can add `env_prefix` widens
    # it to `dict[str, object]`, which the downstream constructors
    # accept but the type checker can't narrow back.
    merged_config: dict[str, object] = {**(config_cls.model_config or {})}  # type: ignore[dict-item]
    merged_config["env_prefix"] = env_prefix
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

    @property
    def config(self) -> ConfigT:
        """Return the current configuration."""
        return self._config

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
            if new_config == self._config:  # pragma: no cover
                return
            await self._apply_reconfigure(new_config)
            self._config = new_config

    async def _apply_reconfigure(self, new_config: ConfigT) -> None:
        """Rebuild cached derived state for `new_config`.

        Runs under `self._reconfigure_lock`. Must not assign
        `self._config`. The default does nothing.
        """
