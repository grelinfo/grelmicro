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
from typing import Annotated, Literal

from pydantic import BaseModel, PositiveFloat, PositiveInt
from typing_extensions import Doc

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
            "has not been hit for ``ttl_seconds``, its counter "
            "resets on the next occurrence. ``None`` (default) "
            "disables time-based expiry; only LRU eviction can "
            "reset a counter."
        ),
    ] = None


def _key_by_rendered(record: LogRecord) -> tuple[str, int, str]:
    """Return ``(logger, level, rendered message)``.

    Falls back to ``str(record.msg)`` if rendering raises.
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

    Keys are tracked in an LRU cache of at most ``cache_size``
    entries. Choose the default key via ``key_mode`` or supply
    ``key`` to override. Set ``ttl_seconds`` to re-emit a burst of
    ``allowed_repetitions`` records every ``ttl_seconds`` during a
    sustained flood, so operators get periodic reminders that the
    issue persists. ``key_mode="template"`` is roughly 3x faster
    than ``"rendered"`` because it skips message formatting.

    Thread-safe: a :class:`threading.Lock` protects the counter
    map. The user-supplied ``key`` callable runs outside the lock.

    State is in-process only; there is no cross-process sharing
    and no reset API. Construct a new filter to wipe counters.
    """

    def __init__(
        self,
        *,
        allowed_repetitions: Annotated[
            PositiveInt,
            Doc(
                "Maximum number of records per key that pass the "
                "filter before subsequent records are dropped."
            ),
        ] = 5,
        cache_size: Annotated[
            PositiveInt,
            Doc(
                "Maximum number of distinct keys tracked. When "
                "exceeded, the least-recently-seen key is evicted."
            ),
        ] = 100,
        key_mode: Annotated[
            KeyMode,
            Doc(
                'Default key strategy: "template" (default) uses '
                '``str(record.msg)``; "rendered" uses '
                "``record.getMessage()``. "
                "Ignored when ``key`` is set."
            ),
        ] = "template",
        ttl_seconds: Annotated[
            PositiveFloat | None,
            Doc(
                "Silence window for automatic counter reset. "
                "``None`` disables time-based expiry."
            ),
        ] = None,
        key: Annotated[
            Callable[[LogRecord], Hashable] | None,
            Doc(
                "Override the default key function. Receives the "
                "record and returns any hashable value."
            ),
        ] = None,
    ) -> None:
        """Initialize the filter."""
        super().__init__()
        self._config = DuplicateFilterConfig(
            allowed_repetitions=allowed_repetitions,
            cache_size=cache_size,
            key_mode=key_mode,
            ttl_seconds=ttl_seconds,
        )
        self._key_fn = key if key is not None else _KEY_FUNCS[key_mode]
        self._counts: OrderedDict[Hashable, tuple[int, float]] = OrderedDict()
        self._lock = Lock()

    @property
    def config(self) -> DuplicateFilterConfig:
        """Return the config."""
        return self._config

    def filter(self, record: LogRecord) -> bool:
        """Return ``True`` if the record should pass, ``False`` to drop."""
        key = self._key_fn(record)
        counts = self._counts
        config = self._config
        allowed = config.allowed_repetitions
        ttl = config.ttl_seconds
        now = monotonic()
        with self._lock:
            entry = counts.get(key)
            if entry is None:
                counts[key] = (1, now)
                if len(counts) > config.cache_size:
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
