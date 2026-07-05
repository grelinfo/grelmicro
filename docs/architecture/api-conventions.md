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

## Components take the provider first, `name` keyword-only

A component or registry is app-level wiring passed to `uses=`, such as
`Coordination`, `Cache`, or `RateLimiterRegistry`. Its first positional is the
provider or backend it wraps. The registration `name` is keyword-only and
defaults to `"default"`:

```python
Coordination(redis)
Cache(redis)
RateLimiterRegistry(redis, name="api")
```

Most apps register one component per kind, so the default name keeps the common
case silent. Name a second instance only when two of the same kind coexist.

## Algorithms use factory classmethods

When a pattern has more than one algorithm, expose each as an explicit factory
classmethod rather than a `kind=` argument. The classmethod names the algorithm
and takes only the fields that algorithm needs:

```python
RateLimiter.sliding_window("api", limit=100, window=60)
RateLimiter.token_bucket("api", rate=10, capacity=20)
CircuitBreaker.consecutive_count("payments", error_threshold=5)
```

The bare constructor stays available for a pre-assembled config object (from
YAML or a `pydantic-settings` tree), but the factory is the path most callers
take.
