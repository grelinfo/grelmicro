# Rate Limit

A `RateLimiter` hands back a `RateLimitResult` and stops. Every service then
writes the same `429` by hand, and most write it wrong: no `Retry-After`, no
quota, a body that does not match what the rest of the app answers with, and a
bucket keyed on the socket peer, which behind an ingress is the ingress.

Take the decision to the edge instead:

```python
--8<-- "http/rate_limit.py"
```

Every request spends one token of every limiter listed. A burst limit stands
beside a daily one, and a request passes both or is turned away by the first
that says no.

## What the caller is told

Allowed or refused, the response states what is left, in the two fields of the
[RateLimit header specification](https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/):

```text
RateLimit: "burst";r=97;t=42, "daily";r=9713;t=51840
RateLimit-Policy: "burst";q=100;w=60, "daily";q=10000;w=86400
```

`r=` is what is left, `t=` the seconds until it resets, `q=` the quota and
`w=` the window. Both fields are lists, which is why two policies fit in one
answer.

A refusal adds `Retry-After` and travels the same `AdmissionError` path every
other rejection takes, so it is rendered by the registered
[`ErrorResponses`](errors.md) as `application/problem+json`, and one `except`
still covers every way a caller is turned away.

`legacy_headers=True` adds `X-RateLimit-Limit`, `-Remaining` and `-Reset`, the
superseded fields, for a client that reads only those. They carry whichever
limiter is closest to being spent.

!!! note "A token bucket states no policy"
    `RateLimit-Policy` describes a quota over a window. A token bucket refills
    continuously, so its reset is the wait for the next token rather than the
    edge of a window, and a policy built from it would promise a reset that
    never comes. A window shorter than a second is left out for the same
    reason: written as the whole second the header takes, it would publish
    half the rate it enforces. Both state `RateLimit` and nothing else.

## Who is metered

The bucket is the resolved client address: the socket peer, unless a proxy you
trust vouched for another one.

```python
RateLimitedRequests(burst, trusted=TrustedProxies(["10.0.0.0/8"]))
```

That is not a detail. A limiter keyed on the peer meters the ingress, so one
caller spends everybody's budget. A limiter keyed on a raw `X-Forwarded-For`
meters whatever the caller wrote there, so nobody spends anything.
[`TrustedProxies`](../security/clientip.md) is what tells the two apart, and
it is required: without it, and without a `key` of your own, the component is
refused where it is written rather than metering the wrong thing.

The address is resolved once per request and left where
`ClientAddressMiddleware` leaves it, so a route that meters itself reads the
same caller.

!!! warning "A bucket is only as good as its key"
    A key a caller controls is a key a caller can rotate. Behind a trusted
    proxy the address is vouched for and cannot be, which is the point of the
    trusted set. In front of one, a spoofed source address is a spoofed
    bucket, and rate limiting is not the tool that stops that.

`key=` replaces the whole thing, for a service that meters by tenant or by API
key. It takes the ASGI scope and returns the bucket, or `None` to leave a
request unmetered, on the component and on a route alike.

## Metering one route

`RateLimitedRequests` meters the app. A route that costs more than the rest,
or has a quota of its own, declares it:

```python
from grelmicro.integrations.fastapi import RateLimited


@app.get("/search", dependencies=[RateLimited(search, cost=5)])
async def do_search(query: str) -> list[Hit]: ...
```

Both budgets are spent, and both are stated in the answer: the standard
fields are lists, so the route's policy and the app's join into one. The
superseded `X-RateLimit-*` fields are single integers that cannot be read
twice, so the meter with less left answers for both: a client reading only
those is told the budget that refuses it first.

`cost=` is how many tokens the call takes, for an endpoint worth more than
one. A cost no limiter could ever serve is refused where it is written rather
than failing every request, on the route and on the app alike. A route that
finds no caller to meter says so once rather than metering nothing in
silence.

## Waiting instead of refusing

`max_wait=0.0` refuses as soon as the budget is spent, which is the default,
because waiting at the edge holds the connection open and a client that
retries is cheaper than a server that queues.

Give a budget where a short wait is better than a retry:

```python
RateLimitedRequests(burst, trusted=..., max_wait=0.5)
```

A budget that runs out is still a refusal: the caller is answered `429` with
the same headers, not an error.

## What is never metered

`exclude=` names the paths that pass through, and takes the same patterns
every grelmicro middleware takes. Put the probes there: Kubernetes polls
`/livez` and `/readyz` every few seconds forever, and that is not a caller's
budget to spend.

A request whose caller cannot be read at all is let through rather than
metered under a bucket that is not theirs, and the reason is logged once.

## When the backend is down

`RateLimiter` decides that, not this middleware. A limiter built with
`fail_open=True` serves the request and reports a full quota, so the headers
say a budget nobody counted. A limiter built without it raises, and the app
answers as it does for any dependency failure. One setting, on the limiter,
wherever it is used.

## Options

Every option of `RateLimitMiddleware` is taken by `RateLimitedRequests` and
forwarded, so a registered component and a hand-added middleware answer the
same.

| Option | What it does |
|---|---|
| `*limiters` | the limiters every request spends |
| `trusted` | the proxies whose forwarded entries may be believed |
| `key` | builds the bucket key itself |
| `cost` | tokens one request spends of each |
| `max_wait` | seconds a throttled request waits before it is refused |
| `exclude` | paths never metered |
| `legacy_headers` | also send the superseded `X-RateLimit-*` fields |
| `name` | keep two sets of rules apart on one app |
