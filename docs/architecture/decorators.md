# Decorator forms

grelmicro decorators follow one rule, so you never have to guess whether to add parentheses.

## The rule

A decorator supports the bare `@deco` form only when every option has a default. When it needs an argument to mean anything, you always call it with parentheses.

- **Bare or parametrized.** `@measure` and `@instrument` work with no arguments and also accept options, so both `@measure` and `@measure(name="checkout")` are valid.
- **Parametrized only.** `@retry`, `@fallback`, `@cached`, and `@interval` need an argument (the retry condition, the fallback value, the cache, the interval), so they are always called with parentheses.

`@shield` is the one bare-first decorator with named presets: use `@shield` for the default, or `@shield.api(...)` / `@shield.internal(...)` / `@shield.slow(...)` for tuned profiles.

## Sync and async

Every decorator wraps both `def` and `async def` functions, except `@shield`, which is async only.

| Decorator | Bare `@deco` | Parametrized `@deco(...)` | Sync | Async |
|-----------|:------------:|:-------------------------:|:----:|:-----:|
| `@measure` | ✓ | ✓ | ✓ | ✓ |
| `@instrument` | ✓ | ✓ | ✓ | ✓ |
| `@shield` | ✓ | ✓ (presets) | | ✓ |
| `@retry(...)` | | ✓ | ✓ | ✓ |
| `@fallback(...)` | | ✓ | ✓ | ✓ |
| `@cached(...)` | | ✓ | ✓ | ✓ |
| `@interval(...)` | | ✓ | ✓ | ✓ |
