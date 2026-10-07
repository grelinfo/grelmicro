# API Conventions

The public API follows a few constructor and factory rules so a new primitive
feels like the existing ones. Follow them when you add a pattern or component.

## Patterns take a positional `name`

A pattern is the object user code calls directly, such as `Lock`,
`CircuitBreaker`, or a `RateLimiter` built from a factory. Its first argument is
a positional `name` that identifies the instance and drives its config prefix:

```python
Lock("cart")
CircuitBreaker("payments")
RateLimiter.sliding_window("api", limit=100, window=60)
```

The name comes first because it is the one argument every call sets. Everything
else (`backend=`, tuning fields) is keyword-only with a default.

## A pattern's name is what it protects

The name is the env prefix and the live-reconfiguration key, so it has to name
the thing being tuned. A pattern that guards an external system takes the
system's name and is shared between call sites. A pattern that shapes one call
takes the call's name:

```python
breaker = CircuitBreaker("recs-api")          # shared by every recs call site

Stack("recs-list", patterns=[
    Fallback("recs-list", when=Exception, default=[]),
    breaker,
    Timeout("recs-list", seconds=1.0),
])
```

`CircuitBreaker` and `RateLimiter` are system-scoped: one circuit and one quota
per dependency, however many call sites reach it. `Retry`, `Timeout`, and
`Fallback` are call-scoped, because what is safe to repeat, how long to wait,
and what to answer with all belong to the call. `Bulkhead` goes either way,
depending on whether it protects the dependency or your own workers.

## Components take the provider first, `name` keyword-only

A component is app-level wiring passed to `uses=`, such as `Coordination`,
`Cache`, or `RateLimiterComponent`. Its first positional is the provider or
backend it wraps. The registration `name` is keyword-only and defaults to
`"default"`:

```python
Coordination(redis)
Cache(redis)
RateLimiterComponent(redis, name="api")
```

Most apps register one component per kind, so the default name keeps the common
case silent. Name a second instance only when two of the same kind coexist.

## Components take the bare capability name

A component is named after the capability it wires, not after its role:
`Cache`, `Coordination`, `Log`, `Trace`, `Metrics`, `Outbox`, `HealthChecks`.
Two carry the `Component` suffix, `RateLimiterComponent` and
`CircuitBreakerComponent`, because their bare names already belong to the
`RateLimiter` and `CircuitBreaker` patterns. The suffix names the concept from
[Backends and Adapters](backends.md), so it adds no vocabulary.

## Algorithms use factory classmethods

When a pattern has more than one algorithm, expose each as an explicit factory
classmethod rather than a `kind=` argument. The classmethod names the algorithm
and takes only the fields that algorithm needs:

```python
RateLimiter.sliding_window("api", limit=100, window=60)
RateLimiter.token_bucket("api", capacity=20, refill_rate=10)
CircuitBreaker.consecutive_count("payments", error_threshold=5)
```

`from_config` is the one door for a pre-assembled config object (from YAML or a
`pydantic-settings` tree). The factory is the path most callers take.

The bare constructor is not a third door. `CircuitBreaker("payments")` works
because the consecutive-count algorithm is a sensible default. `RateLimiter`
has no default algorithm, so it has no bare constructor: both algorithms need
parameters the library cannot guess, which makes naming one part of building
the object.

## The OpenAPI schema has two words, for two things

`include_in_schema=` says whether a router grelmicro builds puts *its own*
routes in the schema. It is FastAPI's word for exactly that, so
`health_router(include_in_schema=True)` reads like the `@app.get(...)` a
reader already knows. It defaults to `False`: an orchestrator, a load
balancer and a scraper read the endpoint, never the schema.

`openapi=` says whether a component annotates *routes you wrote*.
`IdempotentRequests(openapi=False)` leaves your operations undescribed, and
adds no route of its own. It defaults to `True`, because a header the
middleware requires is part of your contract.

Two words because they are two operations. Adding a route to the schema and
annotating someone else's are not the same act, and a component that serves
no route has nothing to include.

## A stored duration is whole seconds or a `timedelta`

A duration that grelmicro stores or enforces takes an `int` of seconds or a
`timedelta`, never a float. A TTL, a lease, a quota window, a ban and a task
schedule are such durations. `RateLimiter.sliding_window("api", limit=100,
window=60)` is the common case, and `window=timedelta(milliseconds=500)`
covers a window under a second.

```python
from datetime import timedelta

from grelmicro.resilience import RateLimiter

RateLimiter.sliding_window("api", limit=100, window=60)
RateLimiter.sliding_window("burst", limit=10, window=timedelta(milliseconds=500))
```

Both are whole microseconds from the start, so nothing has to guess what
was meant. A float such as `1.001` is `1000999.9999999999` microseconds.

From text, such as an environment variable, a duration reads whole seconds
(`"60"`) or an ISO 8601 duration in weeks, days, hours, minutes and seconds
(`"PT0.5S"`, `"P1DT12H"`). Only the seconds take a fraction, up to the
microsecond. Years and months are refused, since their length depends on the
calendar. A config dumped to JSON writes a duration the same way, in days and
smaller units (`"P400D"`), so it reads back exactly. A duration is greater
than zero and at most 100 years.

Once validated, the config holds a `timedelta`, and its field is typed
`timedelta`. A component parameter is typed `int | timedelta`, so
`RateLimiter.sliding_window("api", limit=100, window=60)` type-checks. A
`*Config` built directly takes a `timedelta` in typed code, since a type
checker reads its parameters from the field types.

`cache_ttl=0` turns that cache off. No other duration accepts zero, and `None`
never means off.

A wait, a timeout passed to I/O and the sleep between two runs of a
background loop stay a float of seconds, the type `asyncio` and HTTP clients
take.

| Parameter | Type | Moved |
| --- | --- | --- |
| `SlidingWindowConfig.window` | `int \| timedelta` | yes |
| `ClientBansConfig.window`, `.duration` | `int \| timedelta` | yes |
| `ConsecutiveCountConfig.reset_timeout` | `int \| timedelta` | yes |
| `LockConfig.lease_duration`, `ReadWriteLockConfig.lease_duration` | `int \| timedelta` | yes |
| `TaskLockConfig.lease_duration`, `.min_hold_duration` | `int \| timedelta` | yes |
| `LeaderElectionConfig.lease_duration`, `.renew_deadline` | `int \| timedelta` | yes |
| `OutboxConfig.lease_duration`, `.keep_delivered` | `int \| timedelta` | not yet |
| `TTLCacheConfig.ttl`, `Cache.ttl(ttl)`, `cached(ttl, stale_ttl)`, `TTLCache.set(ttl, stale_ttl)`, `.get_or_set(ttl, stale_ttl)`, `.set_many(ttl)` | `int \| timedelta` | yes |
| `IdempotencyConfig.ttl`, `CachedResponsesConfig.ttl`, `.include` per-path TTLs, `CachedResponse(ttl)` | `int \| timedelta` | yes |
| `DuplicateFilterConfig.ttl` | `int \| timedelta` | not yet |
| `HealthChecksConfig.cache_ttl` | `int \| timedelta` | not yet |
| `JWKSConfig.ttl`, `.cache_ttl`, `DiscoveryConfig.ttl`, `.cache_ttl`, `JWTKeysConfig.cache_ttl` | `int \| timedelta` | yes |
| `OAuthClientConfig.refresh_before`, `.default_lifetime` | `int \| timedelta` | yes |
| `TaskRouter.every(seconds)`, renamed `interval` | `int \| timedelta` | yes |
| cron `misfire_grace_seconds`, renamed `misfire_grace` | `int \| timedelta` | yes |
| `max_wait` on the rate limiter and the bulkhead | `float` | stays |
| `retry_interval`, `error_interval`, `poll_interval`, `export_interval` | `float` | stays |
| `timeout`, `wait_timeout`, `backend_timeout`, `request_timeout` | `float` | stays |
| `shutdown_timeout`, `export_timeout`, `command_timeout` | `float` | stays |
| `TimeoutConfig.seconds` | `float` | stays |
| `RetryConfig.max_seconds`, backoff delays, outbox `retry_base` and `retry_max` | `float` | stays |
