# Decorator forms

grelmicro decorators follow one rule, so you never have to guess whether to add parentheses.

## The rule

A decorator supports the bare `@deco` form only when every option has a default. When it needs an argument to mean anything, you always call it with parentheses.

- **Bare or parametrized.** `@measure` and `@instrument` work with no arguments and also accept options, so both `@measure` and `@measure(name="checkout")` are valid.
- **Parametrized only.** `@retry`, `@fallback`, `@cached`, and `@every` need an argument (the retry condition, the fallback value, the cache, the interval), so they are always called with parentheses.

A pattern instance is a decorator too. `@retrier`, `@breaker`, `@limiter`, `@pool`, `@call_timeout`, and a `@stack` all wrap a function with the pattern you already built and named. `@limiter(key=...)` takes options, and `Stack` is built before it decorates, so it is never called at the decoration site.

`@shield` is the one bare-first decorator with named presets: use `@shield` for the default, or `@shield.api(...)` / `@shield.internal(...)` / `@shield.slow(...)` for tuned profiles.

## Sync and async

Every decorator wraps both `def` and `async def` functions, except `@shield` and `@limiter`, which are async only.

| Decorator | Bare `@deco` | Parametrized `@deco(...)` | Sync | Async |
|-----------|:------------:|:-------------------------:|:----:|:-----:|
| `@measure` | ✓ | ✓ | ✓ | ✓ |
| `@instrument` | ✓ | ✓ | ✓ | ✓ |
| `@shield` | ✓ | ✓ (presets) | | ✓ |
| `@retry(...)` | | ✓ | ✓ | ✓ |
| `@fallback(...)` | | ✓ | ✓ | ✓ |
| `@cached(...)` | | ✓ | ✓ | ✓ |
| `@every(...)` | | ✓ | ✓ | ✓ |
| `@limiter` | ✓ | ✓ | | ✓ |
| `@stack` | ✓ | | ✓ [^1] | ✓ |

[^1]: A `Stack` decorates a sync function only when every pattern in it can. `RateLimiter`, `Bulkhead`, and `Timeout` are async only, and a stack holding one refuses a sync function at decoration.
