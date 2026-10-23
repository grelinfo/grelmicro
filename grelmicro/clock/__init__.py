"""Clock abstraction for time-dependent primitives.

`monotonic()` and `sleep()` resolve the active clock. Production code uses the
real clock with no setup. Tests install a `VirtualClock` to drive time-dependent
behavior without real waiting.

Each name loads its submodule on first access.
"""

import typing as _typing

if _typing.TYPE_CHECKING:
    from grelmicro.clock._protocol import ClockBackend
    from grelmicro.clock._real import RealClock
    from grelmicro.clock._seam import monotonic, sleep
    from grelmicro.clock._virtual import VirtualClock

__all__ = [
    "ClockBackend",
    "RealClock",
    "VirtualClock",
    "monotonic",
    "sleep",
]

_LAZY: dict[str, str] = {
    "ClockBackend": "grelmicro.clock._protocol",
    "RealClock": "grelmicro.clock._real",
    "VirtualClock": "grelmicro.clock._virtual",
    "monotonic": "grelmicro.clock._seam",
    "sleep": "grelmicro.clock._seam",
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
