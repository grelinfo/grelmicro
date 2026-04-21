"""Rate-limit log filter.

Stdlib `logging.Filter` that drops records when a per-key token
bucket is empty. Burst-friendly: allows up to `capacity` records
in a burst, then refills at `refill_rate` records per second.

Industry practice: many logging frameworks ship a burst-style
rate limiter using the token-bucket algorithm for the same
reasons (simple burst semantics, predictable refill).
"""

from collections.abc import Callable
from logging import Filter, LogRecord
from typing import Annotated, Literal

from pydantic import BaseModel, PositiveFloat
from typing_extensions import Doc

from grelmicro.resilience import MemoryTokenBucket

KeyMode = Literal["logger", "level", "global", "template", "rendered"]


class RateLimitFilterConfig(BaseModel, frozen=True, extra="forbid"):
    """Rate Limit Filter Config."""

    capacity: Annotated[
        PositiveFloat,
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
            "Tokens consumed per record. Increase to make some filters "
            "(e.g. attached to a verbose-level handler) spend the "
            "bucket faster than others."
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

    Thread-safe: state lives in a
    [`MemoryTokenBucket`][grelmicro.resilience.MemoryTokenBucket]
    protected by a [`threading.Lock`][]. The user-supplied `key`
    callable runs outside the lock.

    State is in-process only; there is no cross-process sharing.
    Construct a new filter to wipe counters, or call
    [`reset`][grelmicro.logging.RateLimitFilter.reset] on a
    specific key.

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
            PositiveFloat,
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
                "Override the default key function. Receives the "
                "record and returns a string key; any returned key "
                "collides with the default key function's output "
                "namespace, so use a distinctive prefix if needed."
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
