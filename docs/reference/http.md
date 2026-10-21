# HTTP

- **Start here**: [HTTP](../http/index.md)
- **The errors**: [Errors](errors.md)
- **The own-port server**: [Ops Server](../http/server.md)

Register `ErrorResponses()` and `micro.install(app)` wires the handler, so a
rejection raised in a route handler answers the client as an
`application/problem+json` body. Import `ProblemDetail` to return one of your
own, and `send_error` to write one from a pure-ASGI middleware.

Register `AuthenticatedRequests(verifier)` and every request is authenticated
with a bearer token, the verified caller in `request.user`. Read
[Authentication](../http/authentication.md).

Register `IdempotentRequests()` and `install` adds `IdempotencyMiddleware`,
which replays a stored response when a request repeats its `Idempotency-Key`.

Register `ConditionalRequests()` and `install` adds the entity tags, so
`check_precondition(etag_of(version))` refuses a write whose `If-Match` moved
on. Read [Conditional Requests](../http/conditional.md).

Register `CachedResponses()`, declare `CachedResponse(ttl=...)` on a route, and
a repeated read is answered from the cache instead of the handler. Read
[Response Cache](../http/cache.md).

Register `RateLimitedRequests(limiter, trusted=...)` and a caller over its
budget is answered `429` at the edge, with the quota it has left. Read [Rate
Limit](../http/rate-limit.md).

Call `route_template(request.scope)` for the route a request reached, the one
the access log names. Read [The route a request took](../http/where.md#the-route-a-request-took).

Register `OpsServer()` on a process that serves no HTTP, and the health probes
and the Prometheus endpoint answer on a port of their own.

::: grelmicro.http
    options:
      members:
        - OpsServer
        - OpsServerConfig
        - OpsServerError
        - ErrorResponses
        - AuthenticatedRequests
        - AuthenticatedRequestsConfig
        - AuthenticatedRequestsMiddleware
        - IdempotentRequests
        - IdempotencyMiddleware
        - StoredResponse
        - ConditionalRequests
        - ConditionalRequestsMiddleware
        - CachedResponses
        - CachedResponsesMiddleware
        - RateLimitedRequests
        - RateLimitMiddleware
        - check_precondition
        - etag_of
        - route_template
        - PreconditionError
        - PreconditionFailedError
        - PreconditionRequiredError
        - RenderedError
        - send_error
        - merge_headers
        - ProblemDetail
        - TMFError
        - PROBLEM_MEDIA_TYPE
        - ERROR_DOCS_BASE
