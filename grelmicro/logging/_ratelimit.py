"""Rate-limit log filter.

A standard library `logging.Filter` that drops records when the
token bucket for a given key is empty. It allows up to
`capacity` records in a burst, then refills at `refill_rate`
records per second.

Many logging libraries offer a similar burst-style rate limiter
based on the token bucket algorithm. The reasons are the same:
simple burst behavior and predictable refill.
"""

from collections.abc import Callable
from logging import Filter, LogRecord
from typing import Annotated, Literal

from pydantic import BaseModel, PositiveFloat, PositiveInt
from typing_extensions import Doc

from grelmicro.resilience import MemoryTokenBucket

KeyMode = Literal["logger", "level", "global", "template", "rendered"]


class RateLimitFilterConfig(BaseModel, frozen=True, extra="forbid"):
    """Rate Limit Filter Config."""

    capacity: Annotated[
        PositiveInt,
        Doc(
            "Maximum burst size: the bucket holds at most this many "
            "tokens. A burst of up to `capacity` records is allowed "
            "before any are dropped."
        ),
    ] = 5
    refill_rate: Annotated[
        PositiveFloat,
        Doc(
            "Tokens (records) replenished per second. Sets the "
            "sustained rate after the initial burst is exhausted."
        ),
    ] = 1
    key_mode: Annotated[
        KeyMode,
        Doc(
            'Default key strategy. `"logger"` (default) buckets per '
            "logger name (each logger gets its own burst budget). "
            '`"level"` buckets per log level. `"global"` uses a '
            'single shared bucket for all records. `"template"` '
            "buckets per (logger, level, `str(record.msg)`): "
            "collapses across arg values of the same template. "
            '`"rendered"` buckets per (logger, level, '
            "`record.getMessage()`): distinguishes fully-rendered "
            "messages. Ignored when `key` is set."
        ),
    ] = "logger"
    cost: Annotated[
        PositiveFloat,
        Doc(
            "Tokens used per record. Increase this to make some "
            "filters drain their bucket faster than others. For "
            "example, a filter on a verbose-level handler."
        ),
    ] = 1.0


def _key_by_logger(record: LogRecord) -> str:
    return record.name


def _key_by_level(record: LogRecord) -> str:
    return f"level:{record.levelno}"


def _key_by_global(record: LogRecord) -> str:  # noqa: ARG001
    return ""


def _key_by_template(record: LogRecord) -> str:
    return f"{record.name}|{record.levelno}|{record.msg!s}"


def _key_by_rendered(record: LogRecord) -> str:
    try:
        rendered = record.getMessage()
    except Exception:  # noqa: BLE001
        # getMessage() can raise (mismatched args, non-string msg);
        # a logging filter must never break logging itself.
        rendered = str(record.msg)
    return f"{record.name}|{record.levelno}|{rendered}"


_KEY_FUNCS: dict[KeyMode, Callable[[LogRecord], str]] = {
    "logger": _key_by_logger,
    "level": _key_by_level,
    "global": _key_by_global,
    "template": _key_by_template,
    "rendered": _key_by_rendered,
}


class RateLimitFilter(Filter):
    """Rate-limit log records with a token bucket.

    Drops records once the per-key bucket is empty. Each key refills
    at `refill_rate` tokens per second, capped at `capacity`.

    Pick the default key strategy via `key_mode` (per-logger,
    per-level, global, template, rendered) or supply a custom `key`
    callable. Tune `capacity` and `refill_rate` to match your
    acceptable burst and sustained log rate.

    Thread-safe. State lives in a
    [`MemoryTokenBucket`][grelmicro.resilience.MemoryTokenBucket].

    State is kept in the current process only. It is not shared
    between processes. Create a new filter to clear all counters,
    or call
    [`reset`][grelmicro.logging.RateLimitFilter.reset] for a
    single key.

    Example:
    ```python
    import logging

    from grelmicro.logging import RateLimitFilter

    logger = logging.getLogger("grelmicro.ingest")
    logger.addFilter(RateLimitFilter(capacity=10, refill_rate=1))
    ```

    Read more in the [Logging](../logging.md) docs.
    """

    def __init__(
        self,
        *,
        capacity: Annotated[
            PositiveInt,
            Doc("Maximum burst size."),
        ] = 5,
        refill_rate: Annotated[
            PositiveFloat,
            Doc("Tokens replenished per second."),
        ] = 1,
        key_mode: Annotated[
            KeyMode,
            Doc(
                'Default key strategy: "logger" (default), "level", '
                '"global", "template" or "rendered". Ignored when '
                "`key` is set."
            ),
        ] = "logger",
        cost: Annotated[
            PositiveFloat,
            Doc("Tokens consumed per record."),
        ] = 1.0,
        key: Annotated[
            Callable[[LogRecord], str] | None,
            Doc(
                "Override the default key function. It receives "
                "the record and returns a string key. Returned "
                "keys share the same namespace as the default "
                "key function. Add a unique prefix if you need "
                "to avoid collisions."
            ),
        ] = None,
    ) -> None:
        """Initialize the filter."""
        super().__init__()
        self._config = RateLimitFilterConfig(
            capacity=capacity,
            refill_rate=refill_rate,
            key_mode=key_mode,
            cost=cost,
        )
        self._key_fn = key if key is not None else _KEY_FUNCS[key_mode]
        self._bucket = MemoryTokenBucket(
            capacity=capacity,
            refill_rate=refill_rate,
        )

    @property
    def config(self) -> RateLimitFilterConfig:
        """Return the config."""
        return self._config

    def filter(self, record: LogRecord) -> bool:
        """Return `True` to keep the record, `False` to drop it."""
        key = self._key_fn(record)
        return self._bucket.try_acquire(key, cost=self._config.cost)

    def reset(
        self,
        key: Annotated[
            str,
            Doc(
                "Identifier to reset. Use the same value the key "
                "function produces for the records you want to clear. "
                'Pass `""` for the `"global"` key mode.'
            ),
        ] = "",
    ) -> None:
        """Restore `key`'s bucket to full capacity."""
        self._bucket.reset(key)
