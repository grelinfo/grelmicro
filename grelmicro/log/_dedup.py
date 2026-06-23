"""Duplicate message filter.

Stdlib :class:`logging.Filter` that caps repeats per key using a
bounded LRU cache, with optional time-based counter reset. See
the logging user guide for semantics, examples, and trade-offs.
"""

from collections import OrderedDict
from collections.abc import Callable, Hashable
from logging import Filter, LogRecord
from threading import Lock
from time import monotonic
from typing import Annotated, Literal, Self

from pydantic import BaseModel, PositiveFloat, PositiveInt
from typing_extensions import Doc

from grelmicro._config import resolve_config
from grelmicro.log.errors import LogSettingsValidationError

KeyMode = Literal["rendered", "template"]


class DuplicateFilterConfig(BaseModel, frozen=True, extra="forbid"):
    """Duplicate Filter Config."""

    allowed_repetitions: Annotated[
        PositiveInt,
        Doc(
            "Maximum number of records per key that pass the filter "
            "before subsequent records are dropped."
        ),
    ] = 5
    cache_size: Annotated[
        PositiveInt,
        Doc(
            "Maximum number of distinct keys tracked. When exceeded, "
            "the least-recently-seen key is evicted."
        ),
    ] = 100
    key_mode: Annotated[
        KeyMode,
        Doc(
            'Default key strategy. "template" (default) uses '
            "``str(record.msg)`` and is ~3x faster; ``%``-style "
            "parameterized calls collapse across argument values. "
            '"rendered" uses ``record.getMessage()`` to '
            "distinguish per-subject."
        ),
    ] = "template"
    ttl_seconds: Annotated[
        PositiveFloat | None,
        Doc(
            "Silence window for automatic counter reset. If a key "
            "has not been seen for ``ttl_seconds``, its counter "
            "resets the next time it appears. ``None`` (default) "
            "disables time-based expiry, so only LRU eviction can "
            "reset a counter."
        ),
    ] = None


def _key_by_rendered(record: LogRecord) -> tuple[str, int, str]:
    """Return ``(logger, level, rendered message)``.

    Uses ``str(record.msg)`` as a fallback if rendering raises
    an exception.
    """
    try:
        rendered = record.getMessage()
    except Exception:  # noqa: BLE001
        rendered = str(record.msg)
    return (record.name, record.levelno, rendered)


def _key_by_template(record: LogRecord) -> tuple[str, int, str]:
    """Return ``(logger, level, raw format template)``.

    Skips ``getMessage()`` so it is ~3x faster than rendered keying.
    """
    return (record.name, record.levelno, str(record.msg))


_KEY_FUNCS: dict[KeyMode, Callable[[LogRecord], Hashable]] = {
    "rendered": _key_by_rendered,
    "template": _key_by_template,
}


class DuplicateFilter(Filter):
    """Drop log records that repeat beyond ``allowed_repetitions``.

    Keys are tracked in an LRU cache with at most ``cache_size``
    entries. Use ``key_mode`` to pick the default key, or pass
    ``key`` to override it.

    Set ``ttl_seconds`` to emit a new burst of
    ``allowed_repetitions`` records every ``ttl_seconds`` during
    a long flood. This gives operators regular reminders that
    the issue is still active.

    ``key_mode="template"`` is about 3 times faster than
    ``"rendered"`` because it skips message formatting.

    Thread-safe.

    State is kept in the current process only. There is no
    reset method. Create a new filter to clear all counters.
    """

    def __init__(
        self,
        *,
        allowed_repetitions: Annotated[
            PositiveInt | None,
            Doc(
                """
                Maximum number of records per key that pass the
                filter before subsequent records are dropped.

                Default: 5. When unset and env reads are enabled (see ``env_load`` and
                ``GREL_ENV_LOAD``), resolves from the environment
                variable ``GREL_DUPLICATE_FILTER_ALLOWED_REPETITIONS``
                if present, otherwise falls back to the
                ``DuplicateFilterConfig`` default.
                """
            ),
        ] = None,
        cache_size: Annotated[
            PositiveInt | None,
            Doc(
                """
                Maximum number of distinct keys tracked. When
                exceeded, the least-recently-seen key is evicted.

                Default: 100.
                """
            ),
        ] = None,
        key_mode: Annotated[
            KeyMode | None,
            Doc(
                """
                Default key strategy: ``"template"`` (default) uses
                ``str(record.msg)``; ``"rendered"`` uses
                ``record.getMessage()``. Ignored when ``key`` is set.
                """
            ),
        ] = None,
        ttl_seconds: Annotated[
            PositiveFloat | None,
            Doc(
                """
                Silence window for automatic counter reset.
                ``None`` (default) disables time-based expiry.
                """
            ),
        ] = None,
        key: Annotated[
            Callable[[LogRecord], Hashable] | None,
            Doc(
                "Override the default key function. Receives the "
                "record and returns any hashable value."
            ),
        ] = None,
        env_prefix: Annotated[
            str | None,
            Doc(
                """
                Override the auto-derived environment variable prefix.

                Default: ``GREL_DUPLICATE_FILTER_``.
                """
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                """
                Whether to read environment variables.

                When None (the default), follow the process-wide
                ``GREL_ENV_LOAD`` flag. Pass True or False to
                override the flag for this construction.
                """
            ),
        ] = None,
    ) -> None:
        """Initialize the filter."""
        config = resolve_config(
            DuplicateFilterConfig,
            explicit=None,
            kwargs={
                "allowed_repetitions": allowed_repetitions,
                "cache_size": cache_size,
                "key_mode": key_mode,
                "ttl_seconds": ttl_seconds,
            },
            env_prefix=env_prefix or "GREL_DUPLICATE_FILTER_",
            env_load=env_load,
            error_type=LogSettingsValidationError,
        )
        Filter.__init__(self)
        self._setup(config, key)

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            DuplicateFilterConfig,
            Doc(
                """
                The pre-built duplicate filter configuration.

                Use this path when the configuration is assembled at
                startup from a settings tree. The environment path
                is bypassed and the config is used as-is.
                """
            ),
        ],
        *,
        key: Annotated[
            Callable[[LogRecord], Hashable] | None,
            Doc(
                "Override the default key function. Receives the "
                "record and returns any hashable value."
            ),
        ] = None,
    ) -> Self:
        """Construct a `DuplicateFilter` from a pre-built `DuplicateFilterConfig`."""
        instance = cls.__new__(cls)
        Filter.__init__(instance)
        instance._setup(config, key)  # noqa: SLF001
        return instance

    def _setup(
        self,
        config: DuplicateFilterConfig,
        key: Callable[[LogRecord], Hashable] | None,
    ) -> None:
        """Wire the validated config and runtime deps onto the instance."""
        self._config = config
        self._allowed = config.allowed_repetitions
        self._ttl = config.ttl_seconds
        self._cache_size = config.cache_size
        self._key_fn = key if key is not None else _KEY_FUNCS[config.key_mode]
        self._counts: OrderedDict[Hashable, tuple[int, float]] = OrderedDict()
        self._lock = Lock()
        self._next_sweep = 0.0

    @property
    def config(self) -> DuplicateFilterConfig:
        """Return the config."""
        return self._config

    def filter(self, record: LogRecord) -> bool:
        """Return ``True`` if the record should pass, ``False`` to drop."""
        key = self._key_fn(record)
        counts = self._counts
        allowed = self._allowed
        ttl = self._ttl
        cache_size = self._cache_size
        now = monotonic()
        with self._lock:
            if ttl is not None and now >= self._next_sweep:
                # Time-bucketed cleanup. On high-cardinality floods the
                # map fills with keys that will never repeat. Dropping
                # entries unseen for longer than ``ttl`` in one pass lets
                # stale keys leave before they force LRU eviction of keys
                # that are still active. Bounded to once per ``ttl`` so
                # the scan cost is amortized off the per-record path.
                cutoff = now - ttl
                stale = [k for k, (_, seen) in counts.items() if seen <= cutoff]
                for stale_key in stale:
                    del counts[stale_key]
                self._next_sweep = now + ttl
            entry = counts.get(key)
            if entry is None:
                counts[key] = (1, now)
                if len(counts) > cache_size:
                    counts.popitem(last=False)
                return True
            count, last_seen = entry
            if ttl is not None and now - last_seen > ttl:
                counts[key] = (1, now)
                counts.move_to_end(key)
                return True
            if count > allowed:
                counts.move_to_end(key)
                return False
            counts[key] = (count + 1, now)
            counts.move_to_end(key)
            return count < allowed
