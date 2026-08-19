# HTTP

- **Start here**: [Error Responses](../http/errors.md)
- **The errors**: [Errors](errors.md)

Register `ErrorResponses()` and `micro.install(app)` wires the handler, so a
rejection raised in a route handler answers the client as an
`application/problem+json` body. Import `ProblemDetail` to return one of your
own, and `send_error` to write one from a pure-ASGI middleware.

::: grelmicro.http
    options:
      members:
        - ErrorResponses
        - RenderedError
        - send_error
        - merge_headers
        - ProblemDetail
        - TMFError
        - PROBLEM_MEDIA_TYPE
        - ERROR_DOCS_BASE
