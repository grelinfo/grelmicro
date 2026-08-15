# Security

The `security` module holds the checks a service runs on an inbound request. grelmicro validates what arrives. It never issues credentials, runs a login flow, or stores users. That is your identity provider's job.

- **[Client IP](clientip.md)**: resolve the real caller behind a reverse proxy, trusting only the `X-Forwarded-For` entries your own proxies appended.

## Quick start

Name your own proxies once at startup, then resolve the caller on every request.

```python
from grelmicro.security import TrustedProxies, resolve_client_address

trusted = TrustedProxies(["10.0.0.0/8"])

client = resolve_client_address(request.scope, trusted)
```

`client.key` is an address the caller cannot choose, so it is safe as a rate limiter bucket or an audit record. Read the [Client IP](clientip.md) guide for what each outcome means.

## What lives here

A microservice checks the request it was handed. It does not run the login. Everything in this module follows that line, which keeps the trust boundary in one place instead of spread across handlers.

Token validation is the next candidate. See the [roadmap](../roadmap.md) for the direction.
