"""Configuration helpers for grelmicro components.

Exposes:

- `resolve_config`: build a validated Pydantic config from a pre-built
  instance or from kwargs merged with environment variables.
- `env_segment`: normalise an instance name into a POSIX env var
  segment.
- `parse_csv_or_json`: coerce an env var string into a list, accepting
  comma-separated or JSON-array form.

The full contract, including the precedence rules and the
name-as-namespace convention, is documented in
`docs/architecture/config.md`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from grelmicro._json import json_loads

if TYPE_CHECKING:
    from collections.abc import Mapping

C = TypeVar("C", bound=BaseModel)

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


def resolve_config(
    config_cls: type[C],
    *,
    explicit: C | None,
    kwargs: Mapping[str, object | None],
    env_prefix: str,
    read_env: bool = True,
) -> C:
    """Build a validated ``config_cls`` from an explicit instance or kwargs and env.

    Resolution has two mutually exclusive modes. If ``explicit`` is
    provided, it is returned as-is and any non-``None`` value in
    ``kwargs`` raises ``TypeError``. Otherwise, ``None`` kwarg values
    are treated as unset and never reach the model, caller-supplied
    non-``None`` kwargs win over environment variables, and
    environment variables win over defaults declared on ``config_cls``.

    The env path constructs a one-off ``BaseSettings`` subclass that
    inherits ``config_cls`` so its validators, ``frozen``, and
    ``extra`` flags are preserved. Only the ``env_prefix`` is added.
    Pass ``read_env=False`` to skip the env path entirely. Validation
    failures surface as ``pydantic.ValidationError``.

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

    if not read_env:
        return config_cls.model_validate(provided)

    settings_cls = _build_settings_cls(config_cls, env_prefix)
    return settings_cls(**provided)  # type: ignore[return-value, arg-type]  # ty: ignore[invalid-return-type, invalid-argument-type]


def _build_settings_cls(
    config_cls: type[C],
    env_prefix: str,
) -> type[BaseSettings]:
    """Create a one-off BaseSettings subclass that reads env vars.

    The dynamic class inherits ``config_cls`` so all fields,
    validators, and ``model_config`` flags (``frozen``, ``extra``)
    are preserved. Only the ``env_prefix`` is added.
    """
    parent_config: dict[str, object] = {**(config_cls.model_config or {})}  # type: ignore[dict-item]
    parent_config["env_prefix"] = env_prefix
    return type(
        f"_{config_cls.__name__}Settings",
        (config_cls, BaseSettings),
        {"model_config": SettingsConfigDict(**parent_config)},  # type: ignore[typeddict-item]
    )
