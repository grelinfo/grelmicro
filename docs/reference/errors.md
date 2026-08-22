# Errors

- **Start here**: [Configuration](../config.md)
- **The contract**: [Configuration internals](../architecture/config.md)

Every error you would handle subclasses `GrelmicroError`, so one `except`
catches any of them. A bad configuration value always raises
`SettingsValidationError`, whichever pattern or component you built.

`EventLoopDeadlockError` is deliberately outside that tree. It is a
`BaseException`, so `except Exception`, a `Retry`, and a `Fallback` all pass it
through. It reports a wiring mistake, and no fallback value stands in for one.

::: grelmicro
    options:
      members:
        - GrelmicroError
        - SettingsValidationError
        - AdmissionError
        - BackendScopeError
        - DependencyNotFoundError
        - MultipleActiveAppsError
        - OutOfContextError
        - AdapterNotRegisteredError
        - EventLoopDeadlockError
        - ProviderNotRegisteredError
        - GrelmicroConfigWarning
        - EnvLoadOffWarning
        - BackendScopeWarning
        - AmbientBindingWarning
        - SentinelPasswordWarning
        - UnknownEnvironmentWarning
