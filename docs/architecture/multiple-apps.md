# Multiple apps in one process

A `Grelmicro` app is a plain async context manager, so you can construct as many as you like. Whether two can be **active at the same time** depends on what they configure.

## The rule

Most components keep their state on the instance: `Coordination`, `Cache`, `RateLimiters`, `CircuitBreakers`, and `HealthChecks` all hold their own backend, so two overlapping apps never touch each other. They run concurrently with no special handling, the same way a web framework lets you instantiate two `App` objects.

`Log` and `Trace` are different. They configure **process-global** state (the stdlib root logger and the OpenTelemetry tracer provider) and restore the previous value on exit. Within one app the exit stack restores in reverse order, so nesting is safe. Across two overlapping apps there is no such ordering guarantee: the first to exit would restore the global and clobber the second app's configuration.

Because they own a single global, these components are singletons: registering a second `Log` (or `Trace`) on the same app raises `ComponentAlreadyRegisteredError`, even under a different name. There is only one root logger to configure, so a second instance has nothing of its own to own.

So grelmicro also blocks the cross-app case that is unsafe: opening a second app that owns `Log` or `Trace` while another app that owns one is still active raises `MultipleActiveAppsError`. Apps without those components overlap freely.

```python
# Fine: neither app owns process-global state.
async with Grelmicro(uses=[Coordination(redis)]):
    async with Grelmicro(uses=[Cache(redis)]):
        ...

# Raises MultipleActiveAppsError: both own the root logger.
async with Grelmicro(uses=[Log()]):
    async with Grelmicro(uses=[Log()]):
        ...
```

## Opting out

Pass `Grelmicro(allow_multiple=True)` when you are sure two active apps will not fight over the same global. The guard then steps aside for that app. Run apps sequentially whenever you can: it is the simplest way to keep logging and tracing predictable.
