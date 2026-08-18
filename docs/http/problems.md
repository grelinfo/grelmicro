# Problem Details

grelmicro knows why it turned a request away. A rate limiter knows when the
budget refills, an open circuit breaker knows when it next tries the
dependency, a full bulkhead knows there is nothing to wait for. This page is
how that reaches the client.

Every rejection is rendered as an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html)
problem detail, the `application/problem+json` body that HTTP APIs use for
errors.

## Wiring

Register `ErrorResponses()`, and `micro.install(app)` wires the handler:

```python title="problems.py"
--8<-- "http/problems.py"
```

It is registered for FastAPI, Starlette, and Litestar. A framework that
serves no HTTP, such as FastStream, ignores it.

**Nothing happens without the component.** grelmicro installs into a
framework you chose, so it does not change how that framework answers an
error unless you ask. Leave `ErrorResponses()` out and a rejection reaches
your framework's own error handling exactly as any other exception does,
which is a `500` unless you handle it yourself.

A handler you registered wins. Whether you register it before or after
`micro.install(app)`, grelmicro adds its own only for the classes you left
alone.

## What a client sees

Over budget:

```http
GET /quote?client=acme HTTP/1.1

HTTP/1.1 429 Too Many Requests
content-type: application/problem+json
cache-control: no-store
retry-after: 2

{
  "type": "https://grelmicro.grel.info/http/problems/#rate-limit-exceeded",
  "title": "Rate limit exceeded",
  "status": 429,
  "detail": "The client sent more requests than the rate limit allows. Wait for the interval in the Retry-After header before sending another.",
  "instance": "/quote",
  "retry_after": 1.4
}
```

`type` is the stable part. Branch on it rather than on the prose, which can
be reworded, or on the status, which several rejections share.

`retry_after` is the extension member, and the useful half of the response.
`Retry-After` carries the same value in whole seconds, rounded up, for a
client that honours the header without reading the body.

## Every rejection

| Error | Status | `type` anchor | Carries |
|---|---|---|---|
| `RateLimitExceededError` | 429 | [`rate-limit-exceeded`](#rate-limit-exceeded) | `retry_after` |
| `CircuitBreakerError` | 503 | [`circuit-breaker-open`](#circuit-breaker-open) | `retry_after` |
| `BulkheadFullError` | 503 | [`bulkhead-full`](#bulkhead-full) | nothing to wait on |
| `WouldBlockError` | 503 | [`lock-unavailable`](#lock-unavailable) | nothing to wait on |
| `LockTimeoutError` | 503 | [`lock-unavailable`](#lock-unavailable) | nothing to wait on |
| `AdmissionError` | 503 | [`request-refused`](#request-refused) | nothing to wait on |
| `DeadlineExceededError` | 504 | [`deadline-exceeded`](#deadline-exceeded) | `timeout` |
| `IdempotencyConflictError` | 422 | [`idempotency-key-reused`](#idempotency-key-reused) | nothing |
| `IdempotencyWaitTimeoutError` | 409 | [`idempotency-in-flight`](#idempotency-in-flight) | `retry_after` |

The handler is registered on the base classes, so a rejection a later release
adds is covered the day it lands. `AdmissionError` is the catch-all: anything
that turns a caller away and has no entry of its own answers `503`.

## What is never rendered

Anything not in the table above stays unhandled, and the framework answers it
with a `500` as before. A backend that is down, a bug in a handler, and a
misconfiguration are server faults, not client problems, and a problem detail
would only dress them up.

A bare builtin `TimeoutError` is left alone, on purpose. grelmicro cannot tell
one it raised from one a database driver or a socket raised underneath your
handler, and claiming a deadline it does not know would be worse than saying
nothing. That is why every wait grelmicro bounds raises an error of its own:
`DeadlineExceededError` for a `Timeout` policy and `LockTimeoutError` for a
bounded acquire. Both subclass the builtin, so an `except TimeoutError` around
either keeps working.

`Shield` is the one exception, and deliberately. Its per-attempt timeout is an
internal retry signal, and when it gives up it re-raises the error that
actually failed, which is often one from the library you called. grelmicro
renders its own rejections, not someone else's error.

## The problem types

### Rate limit exceeded { #rate-limit-exceeded }

`429`. The caller is over the budget of a `RateLimiter`. `retry_after` is the
seconds until the next request is allowed. Wait that long, then retry.

### Circuit breaker open { #circuit-breaker-open }

`503`. A `CircuitBreaker` in front of a dependency is open, so the call was
refused without trying. `retry_after` is the seconds until the breaker next
admits a probe, counted on the backend that holds the state, so every replica
reports the same answer.

A breaker an operator has forced open carries no `retry_after`. Nothing but an
explicit reset releases it, and inviting a retry would be a lie.

### Concurrency limit reached { #bulkhead-full }

`503`. A `Bulkhead` has no free permit. There is no `retry_after`, because
nothing frees at a known time. Retry with a backoff of your own, or shed the
work.

### Lock held elsewhere { #lock-unavailable }

`503`. A `Lock` acquire did not get in. Another holder has it, and either the
caller asked not to wait (`WouldBlockError`) or waited and ran out
(`LockTimeoutError`). One `type` for both, because a client branching on it
wants the fact they share. The `detail` says which happened.

There is no `retry_after`. A lock frees when its holder is done, which is not
a time grelmicro knows.

### Request refused { #request-refused }

`503`. A rejection under `AdmissionError` with no entry of its own. Reaching
this means grelmicro turned the request away for a reason it has not given a
type yet.

### Deadline exceeded { #deadline-exceeded }

`504`. A `Timeout` deadline elapsed. `timeout` is the deadline in seconds, so
the client learns the wall it hit was the service's own and not the network.
Retrying the same work unchanged hits it again.

### Idempotency key reused { #idempotency-key-reused }

`422`. The same `Idempotency-Key` arrived with a different request payload.
Use a fresh key, or resend the original payload.

### Idempotent request in flight { #idempotency-in-flight }

`409`. A request with the same `Idempotency-Key` is still running, and the
wait for it ran out. `retry_after` is a short hint. Come back and you either
read the stored response or are told to wait again.

### Idempotency key invalid { #idempotency-key-invalid }

`400`. The `Idempotency-Key` header is missing on a route that requires one,
or longer than 255 characters.

### Request body too large { #request-body-too-large }

`413`. `IdempotencyMiddleware` fingerprints the request body and this one is
over `max_body_size`.

## What is safe to show

A problem detail is written for a client you do not control, so grelmicro
writes every `detail` itself and never puts the exception message on the wire.
Those messages name the thing that was refused: the rate limit key, the
breaker, the timeout policy. `RateLimitExceededError` alone would publish the
key it rejected, which is often a client address or a user id.

The same rule applies to what is left out. A problem detail carries the delay
a client can act on, and never the name, the backend, or the limit behind it.

Every problem response also carries:

- `Cache-Control: no-store`, because a refusal is about one client at one
  moment. A shared cache that kept a `429` would serve it to callers who are
  within their budget.
- `X-Content-Type-Options: nosniff`, because `instance` reflects the request
  path back. The media type already says the body is data, and this stops a
  client that guesses otherwise.

## The TM Forum format

A service answering to a telco or OSS platform built on TM Forum Open APIs
speaks a different error body. `ErrorResponses.tmf()` renders it:

```python
micro = Grelmicro(uses=[ErrorResponses.tmf()])
```

```http
HTTP/1.1 429 Too Many Requests
content-type: application/json
retry-after: 2

{
  "code": "GREL-RATE-LIMIT-EXCEEDED",
  "reason": "Rate limit exceeded",
  "message": "The client sent more requests than the rate limit allows. Wait for the interval in the Retry-After header before sending another.",
  "referenceError": "https://grelmicro.grel.info/http/problems/#rate-limit-exceeded",
  "@type": "Error"
}
```

**The statuses are the same.** TMF630 mandates the IANA registry and names
`422`, `429` and `503` itself, so nothing is remapped and `Retry-After` keeps
its meaning. Only the body changes.

`code` is mandatory in TMF630 and its values are left to the API. grelmicro
derives it from the same slug the `type` URI uses, so there is no second
catalogue of identifiers to keep in step. The prefix says which system
defined the code, since your own business codes share the field:

```python
ErrorResponses.tmf(code_prefix="SBB")   # SBB-RATE-LIMIT-EXCEEDED
```

Two things do not survive the format. TMF630 has no equivalent of `instance`,
and it defines no extension member, so `retry_after` reaches the client only
as the `Retry-After` header.

`referenceError` points at this page by default. Point it at your own
documentation, or leave it out for a service whose responses must name no
address outside it:

```python
ErrorResponses.tmf(reference_error="https://docs.example.com/errors/")
ErrorResponses.tmf(reference_error=None)
```

`document_idempotency(app)` follows whichever format is registered, so the
OpenAPI schema publishes `TMFError` rather than `ProblemDetail` when this one
is.

## Returning one yourself

`ProblemDetail` is a plain Pydantic model. Use it for your own errors and the
whole API answers in one shape:

```python
from fastapi import Response

from grelmicro.http import PROBLEM_MEDIA_TYPE, ProblemDetail


@app.post("/charge", responses={409: {"model": ProblemDetail}})
async def charge(amount: int) -> Response:
    if amount > balance:
        problem = ProblemDetail(
            type="https://example.com/problems/insufficient-funds",
            title="Insufficient funds",
            status=409,
            detail="The account does not hold enough to cover this charge.",
            balance=balance,
        )
        return Response(
            content=problem.model_dump_json(exclude_none=True),
            status_code=409,
            media_type=PROBLEM_MEDIA_TYPE,
        )
    ...
```

Anything beyond the five standard members, `balance` here, is kept as an
extension member and serialized at the top level, which is what RFC 9457 asks
for.

## From a middleware

A middleware runs outside the routing layer, so no exception handler sees what
it decides. `send_problem` writes the same body from raw ASGI:

```python
from grelmicro import AdmissionError
from grelmicro.http import problem_detail, send_problem


class Gate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        except AdmissionError as exc:
            problem = problem_detail(exc, instance=scope["path"])
            await send_problem(send, problem)
```

`IdempotencyMiddleware` answers this way, so its `400`, `409`, `413`, and
`422` responses carry the same shape as a rejection raised in a handler.

## OpenAPI

`document_idempotency(app)` publishes the middleware's responses in the
schema, each pointing at a `ProblemDetail` component, so a generated client
knows the body it will get.

For a route that raises a rejection itself, declare it:

```python
@app.get("/quote", responses={429: {"model": ProblemDetail}})
async def quote(client: str) -> dict[str, int]: ...
```
