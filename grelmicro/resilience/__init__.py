"""Resilience.

Pick the front door first, the algorithm second, the backend third.

**Front doors** (start here):

* `RateLimiter.token_bucket(...)` or
  `RateLimiter.sliding_window(...)` for rate limiting.
* `CircuitBreaker("name")` or
  `CircuitBreaker.consecutive_count(...)` for circuit breaking.
* `Retry("name", backoff, when=...)` or the `@retry(...)` /
  `retrying(...)` decorator and block form.
* `Fallback("name", when=..., default=...)` or the `@fallback(...)`
  / `falling_back(...)` decorator and block form.
* `Timeout("name", seconds=...)` for deadlines.
* `Bulkhead("name", max_concurrent=...)` or the `@bulkhead`
  decorator to cap concurrent in-flight calls.
* `Shield("name")` or the `@shield(...)` decorator for the bundled
  timeout + retry + adaptive rate-limit + cache + fallback profile.

**Components** (wire the front doors into `Grelmicro(uses=[...])`):

* `RateLimiterRegistry(backend)` and `CircuitBreakerRegistry(backend)`. They
  register the shared storage. Without them, pass `backend=` on the
  primitive (a memory adapter is fine for tests and single-replica
  services).

**Adapters / backends** (one per storage choice, used inside
`RateLimiterRegistry` / `CircuitBreakerRegistry`): `MemoryRateLimiterAdapter`,
`RedisRateLimiterAdapter`, `PostgresRateLimiterAdapter`,
`SQLiteRateLimiterAdapter`, `MemoryCircuitBreakerAdapter`,
`RedisCircuitBreakerAdapter`, `PostgresCircuitBreakerAdapter`. End
users rarely name these directly. The Components do.

**Configs** (frozen Pydantic models, accept env vars):
`TokenBucketConfig`, `SlidingWindowConfig`,
`CircuitBreakerConfig`, `RetryConfig`, `FallbackConfig`,
`TimeoutConfig`, `BulkheadConfig`, `ShieldConfig`. One per pattern,
plus backoff configs (`ExponentialBackoff`, `LinearBackoff`, ...).

**Loading**: top-level re-exports are PEP 562 lazy. Importing this
package loads `_components`, `_match`, `_outcome`, `_protocol`,
and `errors` plus the eager exports listed below. Every other
pattern, its algorithm configs, and the memory/redis adapters
load on first attribute access. `from grelmicro.resilience import
CircuitBreaker` does not import anything related to `RateLimiter`.

Eager exports (loaded at package import because the function name
shadows a submodule of the same name): `retry`, `retrying`,
`fallback`, `falling_back`, `shield`. The `shield` import pulls the
full `grelmicro.resilience.shield` subpackage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from grelmicro.resilience._components import (
    CircuitBreakerRegistry,
    RateLimiterRegistry,
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
from grelmicro.resilience.errors import (
    BulkheadFullError,
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

# `shield` follows the same shadow rule as `retry`: it would otherwise
# collide with the `grelmicro.resilience.shield` subpackage name.
from grelmicro.resilience.shield import shield

if TYPE_CHECKING:
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
    from grelmicro.resilience.ratelimiter.sqlite import (
        SQLiteRateLimiterAdapter,
    )
    from grelmicro.resilience.retry import (
        Retry,
        RetryAttempt,
        RetryConfig,
        retry,
        retrying,
    )
    from grelmicro.resilience.shield import (
        ApiShieldConfig,
        InternalShieldConfig,
        Shield,
        ShieldConfig,
        SlowShieldConfig,
        shield,
    )
    from grelmicro.resilience.timeout import Timeout, TimeoutConfig

__all__ = [
    "ApiShieldConfig",
    "Bulkhead",
    "BulkheadConfig",
    "BulkheadFullError",
    "CircuitBreaker",
    "CircuitBreakerBackend",
    "CircuitBreakerConfig",
    "CircuitBreakerError",
    "CircuitBreakerMetrics",
    "CircuitBreakerRegistry",
    "CircuitBreakerSnapshot",
    "CircuitBreakerState",
    "CircuitBreakerStrategy",
    "ConsecutiveCountConfig",
    "ConstantBackoff",
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
    "PostgresCircuitBreakerAdapter",
    "PostgresRateLimiterAdapter",
    "RandomBackoff",
    "RateLimitExceededError",
    "RateLimitResult",
    "RateLimiter",
    "RateLimiterBackend",
    "RateLimiterConfig",
    "RateLimiterRegistry",
    "RateLimiterStrategy",
    "RedisCircuitBreakerAdapter",
    "RedisRateLimiterAdapter",
    "ResilienceError",
    "ResilienceSettingsValidationError",
    "Retry",
    "RetryAttempt",
    "RetryBackoffConfig",
    "RetryConfig",
    "RetryStrategy",
    "SQLiteRateLimiterAdapter",
    "Shield",
    "ShieldConfig",
    "SlidingWindowConfig",
    "SlowShieldConfig",
    "Timeout",
    "TimeoutConfig",
    "TokenBucketConfig",
    "fallback",
    "falling_back",
    "retry",
    "retrying",
    "shield",
]

# (attribute -> (module, attribute)). The module is loaded lazily on
# first access. Adding a new Pattern means adding one row per export.
_LAZY: dict[str, tuple[str, str]] = {
    # Bulkhead
    "Bulkhead": ("grelmicro.resilience.bulkhead", "Bulkhead"),
    "BulkheadConfig": ("grelmicro.resilience.bulkhead", "BulkheadConfig"),
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
    "PostgresCircuitBreakerAdapter": (
        "grelmicro.resilience.circuitbreaker.postgres",
        "PostgresCircuitBreakerAdapter",
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
    "SQLiteRateLimiterAdapter": (
        "grelmicro.resilience.ratelimiter.sqlite",
        "SQLiteRateLimiterAdapter",
    ),
    # Retry
    "Retry": ("grelmicro.resilience.retry", "Retry"),
    "RetryAttempt": ("grelmicro.resilience.retry", "RetryAttempt"),
    "RetryConfig": ("grelmicro.resilience.retry", "RetryConfig"),
    # Fallback
    "Fallback": ("grelmicro.resilience.fallback", "Fallback"),
    "FallbackConfig": ("grelmicro.resilience.fallback", "FallbackConfig"),
    "FallbackResult": ("grelmicro.resilience.fallback", "FallbackResult"),
    # Shield
    "Shield": ("grelmicro.resilience.shield", "Shield"),
    "ShieldConfig": ("grelmicro.resilience.shield", "ShieldConfig"),
    "ApiShieldConfig": ("grelmicro.resilience.shield", "ApiShieldConfig"),
    "InternalShieldConfig": (
        "grelmicro.resilience.shield",
        "InternalShieldConfig",
    ),
    "SlowShieldConfig": ("grelmicro.resilience.shield", "SlowShieldConfig"),
    # Timeout
    "Timeout": ("grelmicro.resilience.timeout", "Timeout"),
    "TimeoutConfig": ("grelmicro.resilience.timeout", "TimeoutConfig"),
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
