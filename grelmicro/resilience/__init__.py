"""Resilience.

Top-level re-exports are PEP 562 lazy: importing this package only
loads the small `_components`, `_match`, `_outcome`, `_protocol`, and
`errors` modules. Patterns (`CircuitBreaker`, `RateLimiter`, `Retry`),
their algorithm configs, and the memory/redis adapters load on first
attribute access. `from grelmicro.resilience import CircuitBreaker`
does not import anything related to `RateLimiter`, and vice versa.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from grelmicro.resilience._components import CircuitBreakers, RateLimiters
from grelmicro.resilience._match import Match, Matcher
from grelmicro.resilience._outcome import Outcome
from grelmicro.resilience._protocol import (
    CircuitBreakerBackend,
    CircuitBreakerSnapshot,
    CircuitBreakerStrategy,
    RateLimiterBackend,
    RateLimiterStrategy,
    RateLimitResult,
    RetryStrategy,
)
from grelmicro.resilience.errors import (
    CircuitBreakerError,
    RateLimitExceededError,
    ResilienceError,
    ResilienceSettingsValidationError,
)

# Same shadow handling as `retry`/`retrying`: ``fallback`` and
# ``falling_back`` collide with the ``grelmicro.resilience.fallback``
# submodule name, so they must be bound eagerly.
from grelmicro.resilience.fallback import fallback, falling_back

# `retry` and `retrying` shadow the `grelmicro.resilience.retry` submodule
# name. Python's import system binds submodules as parent-package
# attributes during import, which would shadow our `__getattr__` lazy
# load. Eagerly import these two factories (and force them onto the
# package attribute) so the user-facing function names always resolve to
# the callables. Loading `retry.py` once is fine here since the module
# is needed for `Retry`, `RetryConfig`, every backoff, etc.
from grelmicro.resilience.retry import retry, retrying

if TYPE_CHECKING:
    from grelmicro.resilience.backoffs import (
        ConstantBackoff,
        ExponentialBackoff,
        FibonacciBackoff,
        LinearBackoff,
        RandomBackoff,
        RetryBackoffConfig,
    )
    from grelmicro.resilience.circuitbreaker import (
        CircuitBreaker,
        CircuitBreakerConfig,
        CircuitBreakerMetrics,
        CircuitBreakerState,
        ConsecutiveCountConfig,
        ErrorDetails,
    )
    from grelmicro.resilience.circuitbreaker.memory import (
        MemoryCircuitBreakerAdapter,
    )
    from grelmicro.resilience.circuitbreaker.redis import (
        RedisCircuitBreakerAdapter,
    )
    from grelmicro.resilience.fallback import (
        Fallback,
        FallbackConfig,
        FallbackResult,
        fallback,
        falling_back,
    )
    from grelmicro.resilience.ratelimiter import (
        RateLimiter,
        RateLimiterConfig,
        SlidingWindowConfig,
        TokenBucketConfig,
    )
    from grelmicro.resilience.ratelimiter.memory import (
        MemoryRateLimiterAdapter,
        MemoryTokenBucket,
    )
    from grelmicro.resilience.ratelimiter.postgres import (
        PostgresRateLimiterAdapter,
    )
    from grelmicro.resilience.ratelimiter.redis import RedisRateLimiterAdapter
    from grelmicro.resilience.retry import (
        Retry,
        RetryAttempt,
        RetryConfig,
        retry,
        retrying,
    )

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerBackend",
    "CircuitBreakerConfig",
    "CircuitBreakerError",
    "CircuitBreakerMetrics",
    "CircuitBreakerSnapshot",
    "CircuitBreakerState",
    "CircuitBreakerStrategy",
    "CircuitBreakers",
    "ConsecutiveCountConfig",
    "ConstantBackoff",
    "ErrorDetails",
    "ExponentialBackoff",
    "Fallback",
    "FallbackConfig",
    "FallbackResult",
    "FibonacciBackoff",
    "LinearBackoff",
    "Match",
    "Matcher",
    "MemoryCircuitBreakerAdapter",
    "MemoryRateLimiterAdapter",
    "MemoryTokenBucket",
    "Outcome",
    "PostgresRateLimiterAdapter",
    "RandomBackoff",
    "RateLimitExceededError",
    "RateLimitResult",
    "RateLimiter",
    "RateLimiterBackend",
    "RateLimiterConfig",
    "RateLimiterStrategy",
    "RateLimiters",
    "RedisCircuitBreakerAdapter",
    "RedisRateLimiterAdapter",
    "ResilienceError",
    "ResilienceSettingsValidationError",
    "Retry",
    "RetryAttempt",
    "RetryBackoffConfig",
    "RetryConfig",
    "RetryStrategy",
    "SlidingWindowConfig",
    "TokenBucketConfig",
    "fallback",
    "falling_back",
    "retry",
    "retrying",
]

# (attribute -> (module, attribute)). The module is loaded lazily on
# first access. Adding a new Pattern means adding one row per export.
_LAZY: dict[str, tuple[str, str]] = {
    # Circuit breaker
    "CircuitBreaker": ("grelmicro.resilience.circuitbreaker", "CircuitBreaker"),
    "CircuitBreakerConfig": (
        "grelmicro.resilience.circuitbreaker",
        "CircuitBreakerConfig",
    ),
    "CircuitBreakerMetrics": (
        "grelmicro.resilience.circuitbreaker",
        "CircuitBreakerMetrics",
    ),
    "CircuitBreakerState": (
        "grelmicro.resilience.circuitbreaker",
        "CircuitBreakerState",
    ),
    "ConsecutiveCountConfig": (
        "grelmicro.resilience.circuitbreaker",
        "ConsecutiveCountConfig",
    ),
    "ErrorDetails": ("grelmicro.resilience.circuitbreaker", "ErrorDetails"),
    "MemoryCircuitBreakerAdapter": (
        "grelmicro.resilience.circuitbreaker.memory",
        "MemoryCircuitBreakerAdapter",
    ),
    "RedisCircuitBreakerAdapter": (
        "grelmicro.resilience.circuitbreaker.redis",
        "RedisCircuitBreakerAdapter",
    ),
    # Rate limiter
    "RateLimiter": ("grelmicro.resilience.ratelimiter", "RateLimiter"),
    "RateLimiterConfig": (
        "grelmicro.resilience.ratelimiter",
        "RateLimiterConfig",
    ),
    "SlidingWindowConfig": (
        "grelmicro.resilience.ratelimiter",
        "SlidingWindowConfig",
    ),
    "TokenBucketConfig": (
        "grelmicro.resilience.ratelimiter",
        "TokenBucketConfig",
    ),
    "MemoryRateLimiterAdapter": (
        "grelmicro.resilience.ratelimiter.memory",
        "MemoryRateLimiterAdapter",
    ),
    "MemoryTokenBucket": (
        "grelmicro.resilience.ratelimiter.memory",
        "MemoryTokenBucket",
    ),
    "PostgresRateLimiterAdapter": (
        "grelmicro.resilience.ratelimiter.postgres",
        "PostgresRateLimiterAdapter",
    ),
    "RedisRateLimiterAdapter": (
        "grelmicro.resilience.ratelimiter.redis",
        "RedisRateLimiterAdapter",
    ),
    # Retry
    "Retry": ("grelmicro.resilience.retry", "Retry"),
    "RetryAttempt": ("grelmicro.resilience.retry", "RetryAttempt"),
    "RetryConfig": ("grelmicro.resilience.retry", "RetryConfig"),
    # Fallback
    "Fallback": ("grelmicro.resilience.fallback", "Fallback"),
    "FallbackConfig": ("grelmicro.resilience.fallback", "FallbackConfig"),
    "FallbackResult": ("grelmicro.resilience.fallback", "FallbackResult"),
    # `retry` and `retrying` are imported eagerly above to win the
    # shadow-conflict with the submodule of the same name.
    # Backoff configs (retry-specific)
    "ConstantBackoff": ("grelmicro.resilience.backoffs", "ConstantBackoff"),
    "ExponentialBackoff": (
        "grelmicro.resilience.backoffs",
        "ExponentialBackoff",
    ),
    "FibonacciBackoff": ("grelmicro.resilience.backoffs", "FibonacciBackoff"),
    "LinearBackoff": ("grelmicro.resilience.backoffs", "LinearBackoff"),
    "RandomBackoff": ("grelmicro.resilience.backoffs", "RandomBackoff"),
    "RetryBackoffConfig": (
        "grelmicro.resilience.backoffs",
        "RetryBackoffConfig",
    ),
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy loader for Pattern modules and their algorithm configs."""
    target = _LAZY.get(name)
    if target is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    module_name, attr = target
    import importlib  # noqa: PLC0415

    module = importlib.import_module(module_name)
    value = getattr(module, attr)
    globals()[name] = value  # cache for subsequent access
    return value


def __dir__() -> list[str]:
    """Include lazy attributes in `dir()` for tab completion."""
    return sorted({*globals(), *__all__})
