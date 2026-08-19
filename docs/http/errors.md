# Error Responses

grelmicro knows why it turned a request away. A rate limiter knows when the
budget refills, an open circuit breaker knows when it next tries the
dependency, a full bulkhead knows there is nothing to wait for. This page is
how that reaches the client.

Every error the app answers with is rendered in one standard format.
[RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html) problem details, the
`application/problem+json` body HTTP APIs use for errors, is the default.
The [TM Forum format](#the-tm-forum-format) of TMF630 is the other, for a
service answering to a TM Forum Open API platform.

## Wiring

Register `ErrorResponses()`, and `micro.install(app)` wires the handler:

```python title="errors.py"
--8<-- "http/errors.py"
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
  "type": "https://grelmicro.grel.info/http/errors/#rate-limit-exceeded",
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
| a request that failed validation | the framework's | [`validation-failed`](#validation-failed) | `errors` |

The handler is registered on the base classes, so a rejection a later release
adds is covered the day it lands. `AdmissionError` is the catch-all: anything
that turns a caller away and has no entry of its own answers `503`.

## Your framework's own errors

Registering the component adopts one format for the whole API, so the errors
your framework raises are reshaped too:

- an `HTTPException` you raise keeps its status, its message, and any header
  it carried, `WWW-Authenticate` on a `401` above all. Only the shape
  changes. The two safety headers below are the exception: grelmicro adds
  them because of the body it renders, so they are not overridden. It renders with `about:blank` as the type, which is what RFC 9457
  says for a problem with no specific kind.
- a request that failed validation becomes a
  [`validation-failed`](#validation-failed) response, keeping the status the
  framework chose.

Answering half the API in one shape and half in another would be the
surprising outcome, so this is not a separate switch. To keep your own
handling for one of them, register a handler for it and grelmicro leaves it
alone:

```python
app.add_exception_handler(HTTPException, my_handler)
micro.install(app)
```

The generated OpenAPI follows too, on FastAPI and Litestar alike. Every error
response the framework described with its own shape is republished with the
media type and model the app now answers with, so a client generated from the
schema decodes what it will actually receive. A response you declared
yourself is left alone, and so is its schema.

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

## What is safe to show

A problem detail is written for a client you do not control, so grelmicro
writes every `detail` itself and never puts the exception message on the wire.
Those messages name the thing that was refused: the rate limit key, the
breaker, the timeout policy. `RateLimitExceededError` alone would publish the
key it rejected, which is often a client address or a user id.

The same rule applies to what is left out. A problem detail carries the delay
a client can act on, and never the name, the backend, or the limit behind it.

Every error response also carries, whatever the app set on the exception:

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
  "referenceError": "https://grelmicro.grel.info/http/errors/#rate-limit-exceeded",
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

## Writing your own errors

Registering a handler is how one error opts out of the shared format.
This is the other case: your own handler *and* the shared shape.
`error_response` builds a response in whichever format the app registered:

```python
from fastapi import Request, Response

from grelmicro.integrations.fastapi import error_response


@app.exception_handler(InsufficientFunds)
async def handle(request: Request, exc: InsufficientFunds) -> Response:
    return error_response(
        request,
        status=409,
        detail="The account does not hold enough to cover this charge.",
        extensions={"balance": exc.balance},
    )
```

The format is read from the app, so a service on `ErrorResponses.tmf()`
answers in TM Forum from here too, with no second place to keep in step. An
app that registered nothing gets RFC 9457.

Litestar has the same helper in `grelmicro.integrations.litestar`.

### Returning a body directly
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

### From a pure-ASGI middleware
A middleware runs outside the routing layer, so no exception handler sees what
it decides. `send_error` writes the app's format from raw ASGI:

```python
from grelmicro import AdmissionError, Grelmicro
from grelmicro.http import send_error


class Gate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        except AdmissionError as exc:
            errors = Grelmicro.current().error_responses
            rendered = errors.render(exc, instance=scope["path"])
            await send_error(send, rendered)
```

`send_error` takes what the app's `ErrorResponses` produced rather than a
body of its own, so a middleware cannot answer in a format the rest of the
app does not speak.

`IdempotencyMiddleware` answers this way, so its `400`, `409`, `413`, and
`422` responses carry the same shape as a rejection raised in a handler.

## OpenAPI

The schema follows the format the app answers in, on FastAPI and Litestar
alike. Every error response the framework described with its own shape is
republished with the registered media type and model, and the models it
published for that shape are dropped once nothing points at them. A response
you declared yourself keeps its schema.

`document_idempotency(app)` does the same for the middleware's own responses,
publishing `ProblemDetail` or `TMFError` to match.

For a route that raises a rejection itself, declare it:

```python
@app.get("/quote", responses={429: {"model": ProblemDetail}})
async def quote(client: str) -> dict[str, int]: ...
```

## Reference: every error kind

One section per kind, and the `type` URI of each dereferences here.

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

### Validation failed { #validation-failed }

The request did not match the shape the endpoint accepts. Raised by the
framework, not by grelmicro, and reshaped so it answers in the same format
as everything else.

**The status is the framework's**, not grelmicro's: FastAPI answers `422`
and Litestar `400`. `422` is the more precise code for a request that is
well formed but semantically wrong, and RFC 9110 section 15.5.21 defines
it, but those projects have already answered this for their users and
grelmicro reshapes an answer rather than overruling it. Branch on the
identifier, which is the same either way.

`errors` carries one entry per part that did not match, with `loc`, `msg`
and `type`. The `input` FastAPI includes by default is dropped: it only
repeats what the client just sent.

In the TM Forum format there is nowhere for a list, so the entries are read
into `message`, which is the member for "more details and corrective actions
related to the error which can be shown to a client user".

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
