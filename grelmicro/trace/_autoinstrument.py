"""Auto-instrumentation wiring for the `Trace` component.

`Trace(instrument=...)` selects which active providers and framework
integrations attach their OpenTelemetry instrumentation against the app's
`TracerProvider`. The selection directive is resolved here; the actual
attachment lives on each `Provider.instrument` and in the framework
integrations. A missing `opentelemetry-instrumentation-*` package degrades to a
no-op (silent under the default, a warning when the target was named).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from grelmicro.providers._base import Provider

_logger = logging.getLogger(__name__)

InstrumentDirective = bool | str | Sequence[str] | Mapping[str, bool]
"""`True`/`False`, a single name, an allow-list, or an all-with-overrides map."""

KNOWN_FRAMEWORKS = frozenset({"fastapi"})
"""Framework integration names valid in an `instrument` directive."""


def explicit_names(directive: InstrumentDirective) -> set[str] | None:
    """Return the names named in the directive, or `None` for a bool."""
    if isinstance(directive, bool):
        return None
    if isinstance(directive, str):
        return {directive}
    if isinstance(directive, Mapping):
        return {str(key) for key in directive}
    return set(directive)


def validate_directive(directive: InstrumentDirective, known: set[str]) -> None:
    """Raise if the directive names a target outside `known`.

    The allow-list, single-name, and map forms are strict: a name that matches
    no active provider or known framework is almost always a typo, so it fails
    loudly instead of silently instrumenting the wrong set. A map value that is
    not a bool is rejected the same way, so a mistaken options dict cannot be
    silently treated as "include".

    Raises:
        TraceSettingsValidationError: If the directive names an unknown target
            or maps a name to a non-bool value.
    """
    from grelmicro.trace.errors import (  # noqa: PLC0415
        TraceSettingsValidationError,
    )

    if isinstance(directive, Mapping):
        bad_values = sorted(
            key
            for key, value in directive.items()
            if not isinstance(value, bool)
        )
        if bad_values:
            msg = (
                f"Trace(instrument=...) maps {bad_values} to non-bool values. "
                f"Map a name to True or False."
            )
            raise TraceSettingsValidationError(msg)
    names = explicit_names(directive)
    if names is None:
        return
    unknown = names - known
    if unknown:
        msg = (
            f"Trace(instrument=...) names unknown targets "
            f"{sorted(unknown)}. Known targets are {sorted(known)}."
        )
        raise TraceSettingsValidationError(msg)


def is_selected(name: str, directive: InstrumentDirective) -> bool:
    """Return whether `name` is selected by the directive.

    - `True`/`False`: all or nothing.
    - single name (`str`): selected only when it matches.
    - allow-list (`Sequence`): selected only when listed.
    - all-with-overrides (`Mapping`): selected unless mapped to `False`.
    """
    if isinstance(directive, bool):
        return directive
    if isinstance(directive, str):
        return name == directive
    if isinstance(directive, Mapping):
        return directive.get(name, True) is not False
    return name in directive


def instrument_providers(
    providers: Sequence[Provider],
    tracer_provider: Any,  # noqa: ANN401
    directive: InstrumentDirective,
) -> list[Provider]:
    """Instrument each selected provider; return the ones instrumented.

    A provider whose `instrument` raises is logged and skipped, so one broken
    instrumentor never blocks app startup. A selected provider that reports it
    could not attach (its instrumentor package is absent) warns only when the
    directive named it explicitly, so the default-on path stays quiet until the
    extras are installed.
    """
    named = explicit_names(directive)
    instrumented: list[Provider] = []
    for provider in providers:
        if not is_selected(provider.short_name, directive):
            continue
        try:
            attached = provider.instrument(tracer_provider)
        except Exception:  # noqa: BLE001
            _logger.warning(
                "Failed to auto-instrument provider '%s'",
                provider.short_name,
                exc_info=True,
            )
            continue
        if attached:
            instrumented.append(provider)
        elif named is not None and provider.short_name in named:
            _logger.warning(
                "Trace named '%s' for instrumentation but no instrumentor "
                "attached. Install its opentelemetry-instrumentation-* package.",
                provider.short_name,
            )
    return instrumented


def uninstrument_providers(providers: Sequence[Provider]) -> None:
    """Reverse `instrument_providers` for the given providers."""
    for provider in providers:
        try:
            provider.uninstrument()
        except Exception:  # noqa: BLE001
            _logger.warning(
                "Failed to un-instrument provider '%s'",
                provider.short_name,
                exc_info=True,
            )
