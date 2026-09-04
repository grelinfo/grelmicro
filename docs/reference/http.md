# HTTP

- **Start here**: [HTTP](../http/index.md)
- **The errors**: [Errors](errors.md)
- **The own-port server**: [Ops Server](../http/server.md)

Register `ErrorResponses()` and `micro.install(app)` wires the handler, so a
rejection raised in a route handler answers the client as an
`application/problem+json` body. Import `ProblemDetail` to return one of your
own, and `send_error` to write one from a pure-ASGI middleware.

Register `IdempotentRequests()` and `install` adds `IdempotencyMiddleware`,
which replays a stored response when a request repeats its `Idempotency-Key`.

Register `ConditionalRequests()` and `install` adds the entity tags, so
`check_precondition(etag_of(version))` refuses a write whose `If-Match` moved
on. Read [Conditional Requests](../http/conditional.md).

Register `OpsServer()` on a process that serves no HTTP, and the health probes
and the Prometheus endpoint answer on a port of their own.

::: grelmicro.http
    options:
      members:
        - OpsServer
        - OpsServerConfig
        - OpsServerError
        - ErrorResponses
        - IdempotentRequests
        - IdempotencyMiddleware
        - StoredResponse
        - ConditionalRequests
        - ConditionalRequestsMiddleware
        - check_precondition
        - etag_of
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
