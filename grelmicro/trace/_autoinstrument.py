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
from collections.abc import Collection, Mapping, Sequence
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


_ENTRY_POINT_GROUP = "opentelemetry_instrumentor"
"""Entry-point group every `opentelemetry-instrumentation-*` package registers."""

_PROVIDER_LIBRARY = {"postgres": "asyncpg"}
"""Map a provider `short_name` to the OTel instrumentor name it covers.

A provider that instruments a library under a different name (the Postgres
provider drives the `asyncpg` instrumentor) is listed here so the library
sweep can skip it. Names that match (`redis`, `valkey`) need no entry.
"""

_CONFLICTS = {"sqlalchemy": "asyncpg"}
"""Instrumentors that double-span the same calls, as `{drop: keep}`.

SQLAlchemy on the asyncpg driver runs through asyncpg's wrapped connection
methods, so both instrumentors emit a span per query. When both are active the
key is dropped in favor of the value (asyncpg, the lower layer in the default
extra). Exclude the kept one to switch.
"""


def provider_library_name(short_name: str) -> str:
    """Return the OTel instrumentor name a provider `short_name` covers."""
    return _PROVIDER_LIBRARY.get(short_name, short_name)


def _instrumentor_entry_points() -> dict[str, Any]:
    """Return installed OTel instrumentor entry points keyed by name."""
    from importlib.metadata import entry_points  # noqa: PLC0415

    return {ep.name: ep for ep in entry_points(group=_ENTRY_POINT_GROUP)}


def installed_instrumentors() -> set[str]:
    """Return the names of every installed OTel library instrumentor."""
    return set(_instrumentor_entry_points())


def instrument_libraries(
    tracer_provider: Any,  # noqa: ANN401
    directive: InstrumentDirective,
    *,
    exclude: Collection[str],
) -> list[Any]:
    """Instrument installed OTel library instrumentors against `tracer_provider`.

    Sweeps the `opentelemetry_instrumentor` entry points so any library the app
    uses through its own client (a SQLAlchemy or asyncpg engine, an httpx
    client) is traced without a grelmicro-managed provider. `exclude` skips a
    library a registered provider already instruments per client and the
    framework integrations own (FastAPI). A conflicting pair (SQLAlchemy and
    asyncpg) is reduced to one to avoid duplicate spans.

    Returns the instrumentor instances attached, for later `uninstrument`.
    """
    entries = _instrumentor_entry_points()
    selected = {
        name
        for name in entries
        if name not in exclude and is_selected(name, directive)
    }
    for drop, keep in _CONFLICTS.items():
        if drop in selected and keep in selected:
            selected.discard(drop)
            _logger.warning(
                "Both '%s' and '%s' instrumentors are active; using '%s' to "
                "avoid duplicate spans. Exclude '%s' from Trace(instrument=...) "
                "to switch.",
                drop,
                keep,
                keep,
                keep,
            )
    instrumented: list[Any] = []
    for name in sorted(selected):
        try:
            instrumentor = entries[name].load()()
            instrumentor.instrument(tracer_provider=tracer_provider)
        except Exception:  # noqa: BLE001
            _logger.warning(
                "Failed to auto-instrument library '%s'", name, exc_info=True
            )
            continue
        instrumented.append(instrumentor)
    return instrumented


def uninstrument_libraries(instrumentors: Sequence[Any]) -> None:
    """Reverse `instrument_libraries` for the given instrumentor instances."""
    for instrumentor in instrumentors:
        try:
            instrumentor.uninstrument()
        except Exception:  # noqa: BLE001
            _logger.warning(
                "Failed to un-instrument library '%s'",
                type(instrumentor).__name__,
                exc_info=True,
            )
