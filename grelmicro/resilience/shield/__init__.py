"""Shield resilience pattern.

Each name loads its submodule on first access.
"""

import typing as _typing

if _typing.TYPE_CHECKING:
    from grelmicro.resilience.shield._api import ApiShieldConfig
    from grelmicro.resilience.shield._decorator import shield
    from grelmicro.resilience.shield._internal import InternalShieldConfig
    from grelmicro.resilience.shield._shield import Shield, ShieldConfig
    from grelmicro.resilience.shield._slow import SlowShieldConfig

__all__ = [
    "ApiShieldConfig",
    "InternalShieldConfig",
    "Shield",
    "ShieldConfig",
    "SlowShieldConfig",
    "shield",
]

_LAZY: dict[str, str] = {
    "ApiShieldConfig": "grelmicro.resilience.shield._api",
    "InternalShieldConfig": "grelmicro.resilience.shield._internal",
    "Shield": "grelmicro.resilience.shield._shield",
    "ShieldConfig": "grelmicro.resilience.shield._shield",
    "SlowShieldConfig": "grelmicro.resilience.shield._slow",
    "shield": "grelmicro.resilience.shield._decorator",
}
"""The submodule each public name loads from."""


def __getattr__(name: str) -> object:
    """Load `name` from its submodule and cache it on the package."""
    module = _LAZY.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    import importlib  # noqa: PLC0415

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the loaded attributes and every public name."""
    return sorted({*globals(), *__all__})
