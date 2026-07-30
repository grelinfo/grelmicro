# Client IP

Behind a reverse proxy, the address your app sees is the proxy's. The real
client is in `X-Forwarded-For`, and reading that header naively is one of
the most reliably exploited mistakes in web software.

`X-Forwarded-For` is **append-only**. Its leftmost entry is whatever the
original caller wrote, so an attacker sets it to anything. Only the
entries your own proxies appended can be believed.

```python title="clientip.py"
--8<-- "clientip/clientip.py"
```

`TrustedProxies` takes your proxies' addresses. It is required, and there
is no wildcard. To trust every peer, pass `["0.0.0.0/0", "::/0"]`, which
someone reviewing a config will notice.

## What it guarantees

`ClientAddress.ip` is **always** an address the request actually came
from, or one a verified trusted proxy vouched for. It is never text from
the header, so it is safe as a rate limiter key or an audit record.

The resolution reads the header only when the connecting peer is itself a
trusted proxy. Otherwise anyone who can reach the app directly forges the
whole chain.

## Why it matters for a rate limiter

Keying on a spoofable value means a caller sends a different
`X-Forwarded-For` per request and never hits the limit. It also means an
attacker can spoof *your* address and get you throttled.

```python
async def api(request: Request) -> dict:
    client = resolve_client_address(request.scope, trusted)
    await limiter.acquire_or_raise(key=client.key if client else "unknown")
    ...
```

Use `.key` rather than `str(client.ip)`. It folds an IPv4-mapped address
onto its IPv4 form, so one caller cannot occupy two buckets by connecting
over a dual-stack socket.

## Reading the outcome

Every result carries a `reason`. Only `RESOLVED` means a trusted proxy
vouched for the address, and everything else means the header could not be
believed, so the verified peer was used instead.

| Reason | What happened |
|---|---|
| `RESOLVED` | A trusted proxy vouched for this address. |
| `NO_FORWARDED_HEADER` | Trusted peer, no header. The peer is the client. |
| `UNTRUSTED_PEER` | The connecting peer is not a trusted proxy, so the header was ignored. |
| `CHAIN_EXHAUSTED` | Every entry was a trusted proxy. |
| `MALFORMED_ENTRY` | An entry was not a valid address. |
| `HOP_LIMIT`, `TOO_MANY_ENTRIES`, `HEADER_TOO_LARGE` | A bound was hit. |

A rate limiter can use `.key` whatever the reason. An audit log should
record the reason, and a rising `CHAIN_EXHAUSTED` or `MALFORMED_ENTRY`
rate is worth an alert: both mean something is sending chains your
topology does not explain.

!!! warning "A missing trusted set is not a safe default"
    `resolve_client_address(scope)` does not exist. The trusted set is a
    required argument, because the correct value depends on your topology
    and no library can guess it. An empty `TrustedProxies([])` is legal
    and means trust nothing, so the peer is always returned.

## One value, read everywhere

Two subsystems resolving the client separately will drift, and one will
end up recording the proxy. `ClientAddressMiddleware` resolves once and
caches the result on the request:

```python
from grelmicro.clientip import ClientAddressMiddleware, TrustedProxies

app.add_middleware(
    ClientAddressMiddleware, trusted=TrustedProxies(["10.0.0.0/8"])
)
```

Handlers then read `request.state.client_address`. It is pure ASGI, so it
works on any ASGI server.

`overwrite_scope_client=True` additionally rewrites `scope["client"]`, for
code that reads `request.client.host` and cannot be changed. It is off by
default, because it discards the verified peer an audit record wants.

## If you only run uvicorn

Uvicorn implements the same algorithm, enabled with `--proxy-headers` and
`--forwarded-allow-ips`. If that is your only deployment target, that is a
fine answer and you do not need this module.

Reach for this when you want the trusted set required rather than
defaulted, the verified peer kept alongside the resolved address, a reason
you can alert on, or one behaviour across uvicorn, hypercorn and granian,
which differ in both algorithm and trust model.

## Behaviour worth knowing

**Every entry trusted.** Most implementations fall back to the leftmost
entry. This one returns the verified peer with `CHAIN_EXHAUSTED`, because
in that case no proxy vouched for the leftmost against an untrusted peer,
which is exactly the value that must not become a rate limiter key.

**A malformed entry stops the walk.** It is not skipped. Skipping lets an
attacker insert padding to shift which entry gets chosen.

**Configuration fails loudly.** `TrustedProxies(["10.0.0.1/8"])` raises,
because it has host bits set. Some servers silently downgrade an entry
like that to a string literal that then matches nothing, leaving you with
an empty trusted set and no error.
