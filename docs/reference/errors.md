# Errors

- **Start here**: [Configuration](../config.md)
- **The contract**: [Configuration internals](../architecture/config.md)

Every grelmicro error subclasses `GrelmicroError`, so one `except` catches any
of them. A bad configuration value always raises `SettingsValidationError`,
whichever pattern or component you built.

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
        - ProviderNotRegisteredError
        - GrelmicroConfigWarning
        - EnvLoadOffWarning
        - BackendScopeWarning
        - AmbientBindingWarning
        - SentinelPasswordWarning
        - UnknownEnvironmentWarning
