# Testing

- **Start here**: [Call recorder](../architecture/testing.md#call-recorder)
- **Common recipes**: `record(backend)` instruments a backend and returns a `CallLog`. Assert with `log.count(method, **kwargs)`. `FakeVerifier(alice=fake_claims("alice", "orders:read"))` stands in for a `JWTVerifier` in an authenticated app.

::: grelmicro.testing
    options:
      members:
        - record
        - CallLog
        - Call
        - FakeVerifier
        - fake_claims
