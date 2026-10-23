"""Resilience.

Pick the front door first, the algorithm second, the backend third.

**Front doors** (start here):

* `RateLimiter.token_bucket(...)` or
  `RateLimiter.sliding_window(...)` for rate limiting, plus the
  `@limiter` decorator to meter a function.
* `CircuitBreaker("name")` or
  `CircuitBreaker.consecutive_count(...)` for circuit breaking.
* `Retry("name", backoff, when=...)` or the `@retry(...)` /
  `retrying(...)` decorator and block form.
* `Fallback("name", when=..., default=...)` or the `@fallback(...)`
  / `falling_back(...)` decorator and block form.
* `Timeout("name", seconds=...)` for deadlines.
* `Bulkhead("name", max_concurrent=...)` or the `@bulkhead`
  decorator to cap concurrent in-flight calls.
* `Shield("name", when=...)` or the `@shield.api(when=...)` decorator for the bundled
  timeout + retry + adaptive rate-limit + cache + fallback profile.
* `Stack("name", patterns=[...])` to apply several of the above to
  one call in the safe order, whatever order you list them in.

**Components** (wire the front doors into `Grelmicro(uses=[...])`):

* Listing a Provider is usually enough: `Grelmicro(uses=[redis])` registers a
  default Component for every kind the Provider serves, and a bare backend in
  `uses=[...]` is wrapped for you. Name `RateLimiterComponent(backend)` or
  `CircuitBreakerComponent(backend)` to register a second instance under its
  own name, or to swap the backend in a test. Without any of them, the front
  doors run per replica, or take an explicit `backend=`.

**Adapters / backends** (one per storage choice, wrapped by the
Components): `MemoryRateLimiterAdapter`,
`RedisRateLimiterAdapter`, `PostgresRateLimiterAdapter`,
`SQLiteRateLimiterAdapter`, `MemoryCircuitBreakerAdapter`,
`RedisCircuitBreakerAdapter`, `PostgresCircuitBreakerAdapter`. End
users rarely name these directly. The Components do.

**Configs** (frozen Pydantic models, accept env vars):
`TokenBucketConfig`, `SlidingWindowConfig`,
`CircuitBreakerConfig`, `RetryConfig`, `FallbackConfig`,
`TimeoutConfig`, `BulkheadConfig`, `ShieldConfig`. One per pattern,
plus backoff configs (`ExponentialBackoff`, `LinearBackoff`, ...).

**Loading**: `retry`, `retrying`, `fallback`, `falling_back` and
`shield` load with the package. Each other name loads its submodule on
first access, so `from grelmicro.resilience import CircuitBreaker` does
not import anything related to `RateLimiter`.
"""

import typing as _typing

# `retry`, `fallback` and `shield` are also submodule names, and importing a
# submodule binds it on the package, so the functions are bound here before
# any lazy load.
from grelmicro.resilience.fallback import fallback, falling_back
from grelmicro.resilience.retry import retry, retrying
from grelmicro.resilience.shield import shield

if _typing.TYPE_CHECKING:
    from grelmicro.resilience._components import (
        CircuitBreakerComponent,
        RateLimiterComponent,
    )
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
    from grelmicro.resilience.backoffs import (
        ConstantBackoff,
        ExponentialBackoff,
        FibonacciBackoff,
        LinearBackoff,
        RandomBackoff,
        RetryBackoffConfig,
    )
    from grelmicro.resilience.bulkhead import Bulkhead, BulkheadConfig
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
    from grelmicro.resilience.circuitbreaker.postgres import (
        PostgresCircuitBreakerAdapter,
    )
    from grelmicro.resilience.circuitbreaker.redis import (
        RedisCircuitBreakerAdapter,
    )
    from grelmicro.resilience.circuitbreaker.sqlite import (
        SQLiteCircuitBreakerAdapter,
    )
    from grelmicro.resilience.errors import (
        BulkheadFullError,
        CircuitBreakerError,
        DeadlineExceededError,
        RateLimitExceededError,
        ResilienceError,
    )
    from grelmicro.resilience.fallback import (
        Fallback,
        FallbackConfig,
        FallbackResult,
    )
    from grelmicro.resilience.ratelimiter import (
        RateLimiter,
        RateLimiterBinding,
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
    from grelmicro.resilience.ratelimiter.sqlite import (
        SQLiteRateLimiterAdapter,
    )
    from grelmicro.resilience.retry import Retry, RetryAttempt, RetryConfig
    from grelmicro.resilience.shield import (
        ApiShieldConfig,
        InternalShieldConfig,
        Shield,
        ShieldConfig,
        SlowShieldConfig,
    )
    from grelmicro.resilience.stack import Pattern, Stack
    from grelmicro.resilience.timeout import Timeout, TimeoutConfig

__all__ = [
    "ApiShieldConfig",
    "Bulkhead",
    "BulkheadConfig",
    "BulkheadFullError",
    "CircuitBreaker",
    "CircuitBreakerBackend",
    "CircuitBreakerComponent",
    "CircuitBreakerConfig",
    "CircuitBreakerError",
    "CircuitBreakerMetrics",
    "CircuitBreakerSnapshot",
    "CircuitBreakerState",
    "CircuitBreakerStrategy",
    "ConsecutiveCountConfig",
    "ConstantBackoff",
    "DeadlineExceededError",
    "ErrorDetails",
    "ExponentialBackoff",
    "Fallback",
    "FallbackConfig",
    "FallbackResult",
    "FibonacciBackoff",
    "InternalShieldConfig",
    "LinearBackoff",
    "Match",
    "Matcher",
    "MemoryCircuitBreakerAdapter",
    "MemoryRateLimiterAdapter",
    "MemoryTokenBucket",
    "Outcome",
    "Pattern",
    "PostgresCircuitBreakerAdapter",
    "PostgresRateLimiterAdapter",
    "RandomBackoff",
    "RateLimitExceededError",
    "RateLimitResult",
    "RateLimiter",
    "RateLimiterBackend",
    "RateLimiterBinding",
    "RateLimiterComponent",
    "RateLimiterConfig",
    "RateLimiterStrategy",
    "RedisCircuitBreakerAdapter",
    "RedisRateLimiterAdapter",
    "ResilienceError",
    "Retry",
    "RetryAttempt",
    "RetryBackoffConfig",
    "RetryConfig",
    "RetryStrategy",
    "SQLiteCircuitBreakerAdapter",
    "SQLiteRateLimiterAdapter",
    "Shield",
    "ShieldConfig",
    "SlidingWindowConfig",
    "SlowShieldConfig",
    "Stack",
    "Timeout",
    "TimeoutConfig",
    "TokenBucketConfig",
    "fallback",
    "falling_back",
    "retry",
    "retrying",
    "shield",
]

_LAZY: dict[str, str] = {
    "ApiShieldConfig": "grelmicro.resilience.shield",
    "Bulkhead": "grelmicro.resilience.bulkhead",
    "BulkheadConfig": "grelmicro.resilience.bulkhead",
    "BulkheadFullError": "grelmicro.resilience.errors",
    "CircuitBreaker": "grelmicro.resilience.circuitbreaker",
    "CircuitBreakerBackend": "grelmicro.resilience._protocol",
    "CircuitBreakerComponent": "grelmicro.resilience._components",
    "CircuitBreakerConfig": "grelmicro.resilience.circuitbreaker",
    "CircuitBreakerError": "grelmicro.resilience.errors",
    "CircuitBreakerMetrics": "grelmicro.resilience.circuitbreaker",
    "CircuitBreakerSnapshot": "grelmicro.resilience._protocol",
    "CircuitBreakerState": "grelmicro.resilience.circuitbreaker",
    "CircuitBreakerStrategy": "grelmicro.resilience._protocol",
    "ConsecutiveCountConfig": "grelmicro.resilience.circuitbreaker",
    "ConstantBackoff": "grelmicro.resilience.backoffs",
    "DeadlineExceededError": "grelmicro.resilience.errors",
    "ErrorDetails": "grelmicro.resilience.circuitbreaker",
    "ExponentialBackoff": "grelmicro.resilience.backoffs",
    "Fallback": "grelmicro.resilience.fallback",
    "FallbackConfig": "grelmicro.resilience.fallback",
    "FallbackResult": "grelmicro.resilience.fallback",
    "FibonacciBackoff": "grelmicro.resilience.backoffs",
    "InternalShieldConfig": "grelmicro.resilience.shield",
    "LinearBackoff": "grelmicro.resilience.backoffs",
    "Match": "grelmicro.resilience._match",
    "Matcher": "grelmicro.resilience._match",
    "MemoryCircuitBreakerAdapter": "grelmicro.resilience.circuitbreaker.memory",
    "MemoryRateLimiterAdapter": "grelmicro.resilience.ratelimiter.memory",
    "MemoryTokenBucket": "grelmicro.resilience.ratelimiter.memory",
    "Outcome": "grelmicro.resilience._outcome",
    "Pattern": "grelmicro.resilience.stack",
    "PostgresCircuitBreakerAdapter": (
        "grelmicro.resilience.circuitbreaker.postgres"
    ),
    "PostgresRateLimiterAdapter": "grelmicro.resilience.ratelimiter.postgres",
    "RandomBackoff": "grelmicro.resilience.backoffs",
    "RateLimitExceededError": "grelmicro.resilience.errors",
    "RateLimitResult": "grelmicro.resilience._protocol",
    "RateLimiter": "grelmicro.resilience.ratelimiter",
    "RateLimiterBackend": "grelmicro.resilience._protocol",
    "RateLimiterBinding": "grelmicro.resilience.ratelimiter",
    "RateLimiterComponent": "grelmicro.resilience._components",
    "RateLimiterConfig": "grelmicro.resilience.ratelimiter",
    "RateLimiterStrategy": "grelmicro.resilience._protocol",
    "RedisCircuitBreakerAdapter": "grelmicro.resilience.circuitbreaker.redis",
    "RedisRateLimiterAdapter": "grelmicro.resilience.ratelimiter.redis",
    "ResilienceError": "grelmicro.resilience.errors",
    "Retry": "grelmicro.resilience.retry",
    "RetryAttempt": "grelmicro.resilience.retry",
    "RetryBackoffConfig": "grelmicro.resilience.backoffs",
    "RetryConfig": "grelmicro.resilience.retry",
    "RetryStrategy": "grelmicro.resilience._protocol",
    "SQLiteCircuitBreakerAdapter": "grelmicro.resilience.circuitbreaker.sqlite",
    "SQLiteRateLimiterAdapter": "grelmicro.resilience.ratelimiter.sqlite",
    "Shield": "grelmicro.resilience.shield",
    "ShieldConfig": "grelmicro.resilience.shield",
    "SlidingWindowConfig": "grelmicro.resilience.ratelimiter",
    "SlowShieldConfig": "grelmicro.resilience.shield",
    "Stack": "grelmicro.resilience.stack",
    "Timeout": "grelmicro.resilience.timeout",
    "TimeoutConfig": "grelmicro.resilience.timeout",
    "TokenBucketConfig": "grelmicro.resilience.ratelimiter",
}
"""The submodule each public name not bound at import loads from."""


def __getattr__(name: str) -> object:
    """Load `name` from its submodule and cache it on the package."""
    module = _LAZY.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    import importlib  # noqa: PLC0415

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the loaded attributes and every public name."""
    return sorted({*globals(), *__all__})
