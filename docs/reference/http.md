# HTTP

- **Start here**: [Problem Details](../http/problems.md)
- **The errors**: [Errors](errors.md)

Register `ProblemDetails()` and `micro.install(app)` wires the handler, so a
rejection raised in a route handler answers the client as an
`application/problem+json` body. Import `ProblemDetail` to return one of your
own, and `send_problem` to write one from a pure-ASGI middleware.

::: grelmicro.http
    options:
      members:
        - ProblemDetails
        - ProblemDetail
        - problem_detail
        - send_problem
        - PROBLEM_MEDIA_TYPE
        - PROBLEM_TYPE_BASE
