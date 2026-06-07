# Testing

- **Start here**: [Call recorder](../architecture/testing.md#call-recorder)
- **Common recipes**: `record(backend)` instruments a backend and returns a `CallLog`. Assert with `log.count(method, **kwargs)`.

::: grelmicro.testing
    options:
      members:
        - record
        - CallLog
        - Call
