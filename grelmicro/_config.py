"""Configuration resolution for grelmicro components.

Exposes :func:`resolve_config`, a helper that merges three sources of
configuration into one validated Pydantic model: explicit kwargs, an
optional pre-built config instance, and environment variables.

The full contract, including the precedence rules and the
name-as-namespace convention, is documented in
``docs/architecture/config.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Mapping

C = TypeVar("C", bound=BaseModel)


def resolve_config(
    config_cls: type[C],
    *,
    explicit: C | None,
    kwargs: Mapping[str, object | None],
    env_prefix: str,
    read_env: bool = True,
) -> C:
    """Build a validated ``config_cls`` from kwargs, an explicit instance, and env.

    Three sources merge with strict precedence. Caller-supplied
    ``kwargs`` win over an ``explicit`` pre-built instance, which
    wins over environment variables, which win over defaults declared
    on ``config_cls``. ``None`` kwarg values are treated as unset and
    never reach the model. Mixing ``explicit`` with any non-None
    kwarg raises ``TypeError`` since the two paths are mutually
    exclusive by design.

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
