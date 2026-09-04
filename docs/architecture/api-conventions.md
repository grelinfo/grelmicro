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
