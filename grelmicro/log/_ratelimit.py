"""Rate-limit log filter.

A standard library `logging.Filter` that drops records when the
token bucket for a given key is empty. It allows up to
`capacity` records in a burst, then refills at `refill_rate`
records per second. Simple burst behaviour and predictable refill.
"""

from collections.abc import Callable
from logging import Filter, LogRecord
from typing import Annotated, Literal, Self

from pydantic import BaseModel, PositiveFloat, PositiveInt
from typing_extensions import Doc

from grelmicro._config import resolve_config
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
    [`reset`][grelmicro.log.RateLimitFilter.reset] for a
    single key.

    Example:
    ```python
    import logging

    from grelmicro.log import RateLimitFilter

    logger = logging.getLogger("grelmicro.ingest")
    logger.addFilter(RateLimitFilter(capacity=10, refill_rate=1))
    ```

    Read more in the [Logging](../logging.md) docs.
    """

    def __init__(
        self,
        *,
        capacity: Annotated[
            PositiveInt | None,
            Doc(
                """
                Maximum burst size.

                Default: 5. When unset and env reads are enabled (see ``read_env`` and
                ``GREL_CONFIG_FROM_ENV``), resolves from the environment
                variable ``GREL_RATE_LIMIT_FILTER_CAPACITY`` if
                present, otherwise falls back to the
                ``RateLimitFilterConfig`` default.
                """
            ),
        ] = None,
        refill_rate: Annotated[
            PositiveFloat | None,
            Doc(
                """
                Tokens replenished per second.

                Default: 1.
                """
            ),
        ] = None,
        key_mode: Annotated[
            KeyMode | None,
            Doc(
                """
                Default key strategy: "logger" (default), "level",
                "global", "template" or "rendered". Ignored when
                ``key`` is set.
                """
            ),
        ] = None,
        cost: Annotated[
            PositiveFloat | None,
            Doc(
                """
                Tokens consumed per record.

                Default: 1.0.
                """
            ),
        ] = None,
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
        env_prefix: Annotated[
            str | None,
            Doc(
                """
                Override the auto-derived environment variable prefix.

                Default: ``GREL_RATE_LIMIT_FILTER_``.
                """
            ),
        ] = None,
        read_env: Annotated[
            bool | None,
            Doc(
                """
                Whether to read environment variables.

                When None (the default), follow the process-wide
                ``GREL_CONFIG_FROM_ENV`` flag. Pass True or False to
                override the flag for this construction.
                """
            ),
        ] = None,
    ) -> None:
        """Initialize the filter."""
        config = resolve_config(
            RateLimitFilterConfig,
            explicit=None,
            kwargs={
                "capacity": capacity,
                "refill_rate": refill_rate,
                "key_mode": key_mode,
                "cost": cost,
            },
            env_prefix=env_prefix or "GREL_RATE_LIMIT_FILTER_",
            read_env=read_env,
        )
        Filter.__init__(self)
        self._setup(config, key)

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            RateLimitFilterConfig,
            Doc(
                """
                The pre-built rate limit filter configuration.

                Use this path when the configuration is assembled at
                startup from a settings tree. The environment path
                is bypassed and the config is used as-is.
                """
            ),
        ],
        *,
        key: Annotated[
            Callable[[LogRecord], str] | None,
            Doc(
                "Override the default key function. It receives "
                "the record and returns a string key."
            ),
        ] = None,
    ) -> Self:
        """Construct a `RateLimitFilter` from a pre-built `RateLimitFilterConfig`."""
        instance = cls.__new__(cls)
        Filter.__init__(instance)
        instance._setup(config, key)  # noqa: SLF001
        return instance

    def _setup(
        self,
        config: RateLimitFilterConfig,
        key: Callable[[LogRecord], str] | None,
    ) -> None:
        """Wire the validated config and runtime deps onto the instance."""
        self._config = config
        self._key_fn = key if key is not None else _KEY_FUNCS[config.key_mode]
        self._bucket = MemoryTokenBucket(
            capacity=config.capacity,
            refill_rate=config.refill_rate,
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
