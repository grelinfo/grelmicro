"""Duplicate message filter.

Modeled after Logback's ``DuplicateMessageFilter``. Keeps an LRU
cache of recent keys and drops records once a key has been seen
more than ``allowed_repetitions`` times.
"""

from collections import OrderedDict
from collections.abc import Callable, Hashable
from logging import Filter, LogRecord
from threading import Lock
from typing import Annotated

from pydantic import BaseModel, PositiveInt
from typing_extensions import Doc


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


def _default_key(record: LogRecord) -> tuple[str, int, str]:
    """Return ``(logger, level, rendered message)`` for a record.

    Uses ``record.getMessage()`` so the key reflects what a human
    reads in the log. This works identically for ``%``-style calls
    (``logger.warning("%s failed", name)``) and f-string calls
    (``logger.warning(f"{name} failed")``): two calls producing the
    same rendered text collapse into the same bucket, while calls
    with different subjects keep distinct counters.

    If rendering fails (for example, a ``%``/``args`` mismatch),
    fall back to ``str(record.msg)`` so a broken log call cannot
    raise from inside the filter.
    """
    try:
        rendered = record.getMessage()
    except Exception:  # noqa: BLE001
        rendered = str(record.msg)
    return (record.name, record.levelno, rendered)


class DuplicateFilter(Filter):
    """Drop log records that repeat beyond an allowed count.

    After a key has been seen ``allowed_repetitions`` times, every
    further record with the same key is silently dropped. Keys are
    tracked in an LRU cache bounded by ``cache_size`` entries; a
    key evicted under size pressure starts fresh the next time it
    appears, so a long-suppressed message may re-emerge once the
    cache has been fully recycled.

    The default key is ``(record.name, record.levelno,
    record.getMessage())`` -- the rendered message -- so two log
    calls collapse when they would print identical text, regardless
    of whether they came from a ``%``-style call or an f-string.
    Pass ``key=`` to fingerprint on other attributes of the record
    (for example, the raw ``record.msg`` template).

    Thread-safe: a :class:`threading.Lock` protects the counter map.
    The user-supplied ``key`` callable runs outside the lock, so it
    must be pure or manage its own synchronization.
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
        )
        self._key_fn = key or _default_key
        self._counts: OrderedDict[Hashable, int] = OrderedDict()
        self._lock = Lock()

    @property
    def config(self) -> DuplicateFilterConfig:
        """Return the config."""
        return self._config

    def filter(self, record: LogRecord) -> bool:
        """Return ``True`` if the record should pass, ``False`` to drop."""
        key = self._key_fn(record)
        counts = self._counts
        allowed = self._config.allowed_repetitions
        with self._lock:
            current = counts.get(key)
            if current is None:
                counts[key] = 1
                if len(counts) > self._config.cache_size:
                    counts.popitem(last=False)
                return True
            if current > allowed:
                counts.move_to_end(key)
                return False
            counts[key] = current + 1
            counts.move_to_end(key)
            return current < allowed
