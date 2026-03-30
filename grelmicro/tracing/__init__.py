"""Tracing."""

from grelmicro.tracing.errors import (
    TracingError,
)

__all__ = [
    "TracingError",
    "instrument",
]


def __getattr__(name: str) -> object:
    if name == "instrument":
        from grelmicro.tracing._instrument import (  # noqa: PLC0415
            instrument,
        )

        return instrument
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
