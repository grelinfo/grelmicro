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

!!! danger "Turn off your server's own proxy handling first"
    Uvicorn rewrites `scope["client"]` from `X-Forwarded-For` **by default**,
    before any application middleware runs. If you leave it on, the address
    this module treats as the verified peer is itself header-derived, and
    every guarantee below is void: a caller who writes
    `X-Forwarded-For: 6.6.6.6, 10.0.0.1` gets `6.6.6.6` back as `RESOLVED`.

    Run uvicorn with `--no-proxy-headers`, or `Config(..., proxy_headers=False)`.
    Hypercorn and granian only apply theirs when you wrap the app, so leave
    those wrappers off.

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

Every result carries a `reason`. `RESOLVED` is the only one this module
can vouch for on its own: a trusted proxy wrote that entry. Every other
value means `ip` is the verified transport peer, and those split in two.

| Reason | What happened | Whose address is `ip` |
|---|---|---|
| `RESOLVED` | A trusted proxy vouched for this address. | The caller |
| `UNTRUSTED_PEER` | The peer is not a trusted proxy, so the header was ignored. | The peer, which is the caller only if nothing unlisted fronts the app |
| `NO_FORWARDED_HEADER` | A trusted peer sent no header. | The peer, which is the caller only if every proxy of yours appends the header |
| `CHAIN_EXHAUSTED` | Every entry was a trusted proxy. | One of your own proxies |
| `MALFORMED_ENTRY` | An entry was not a valid address. | One of your own proxies |
| `HOP_LIMIT`, `TOO_MANY_ENTRIES`, `HEADER_TOO_LARGE` | A bound was hit. | One of your own proxies |

The right-hand column is the part no library can decide for you. Whether
something sits in front of your app, and whether it appends
`X-Forwarded-For`, are facts about your deployment.

An audit log should record the reason. A rising `CHAIN_EXHAUSTED`,
`TOO_MANY_ENTRIES` or `MALFORMED_ENTRY` rate is worth an alert, since all
three mean something is sending chains your topology does not explain.

### Which check to use

A rate limiter can use `.key` whatever the reason, because every value is
an address the caller cannot choose.

Anything that treats the address as an identity is different. An
allowlist, a private network gate, or any check that has to keep callers
apart needs the address a proxy vouched for:

```python
if client is None or not client.forwarded:
    return False  # nobody vouched for this address
```

`degraded` is the weaker test. It is True only when `ip` is one of your
own proxies, so it catches a chain that could not be walked and misses a
proxy missing from `TrustedProxies`. One mistyped CIDR turns every
request into `UNTRUSTED_PEER`, where the address is the proxy's and
`degraded` is False. `forwarded` refuses that request, `degraded` admits
it, and every caller then looks like the same private client.

If nothing fronts your app, `forwarded` is never True and there is
nothing to gate on. The peer is the caller, verified by the transport,
and `TrustedProxies([])` says exactly that. One flag cannot serve both
deployments, so pick the check that matches yours.

!!! warning "A missing trusted set is not a safe default"
    `resolve_client_address(scope)` does not exist. The trusted set is a
    required argument, because the correct value depends on your topology
    and no library can guess it. An empty `TrustedProxies([])` is legal
    and means trust nothing, so the peer is always returned.

## Catching a mistyped trusted set

A proxy left out of the set has no symptom of its own. Every request
resolves as `UNTRUSTED_PEER` carrying the proxy's own address, which
looks like a real answer. So an untrusted peer that sends a non-empty
`X-Forwarded-For` gets a line on the `grelmicro.clientip` logger:

```text
Ignored X-Forwarded-For from 192.168.1.10, which is not a trusted proxy.
```

One line per peer, for at most eight peers, then silence. A busy proxy
cannot flood the log, and a caller probing the header cannot take the
line your own proxy needs. An empty header is not counted, so direct
health checks stay quiet. Nothing is logged at all when the trusted set
is empty, since that deployment means trust nothing.

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

Handlers then read `getattr(request.state, "client_address", None)`,
which is None when the peer could not be resolved at all. It is pure ASGI,
so it works on any ASGI server.

`overwrite_scope_client=True` additionally rewrites `scope["client"]`, for
code that reads `request.client.host` and cannot be changed. It is off by
default, because it discards the verified peer an audit record wants.

## If you only run uvicorn

Uvicorn does something similar, controlled by `--forwarded-allow-ips`, and
it is **on unless you disable it**. If uvicorn is your only deployment
target, configuring it is a fine answer and you do not need this module.

It is not the same algorithm. Under `--forwarded-allow-ips='*'` uvicorn
returns the leftmost entry, it accepts values that are not addresses at
all, and it reports port `0`.

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

**A capped header says so.** Reading stops at `max_entries`, keeping the
rightmost entries. If every entry read was a trusted proxy, the reason is
`TOO_MANY_ENTRIES` rather than `CHAIN_EXHAUSTED`, because the entries
past the cap were never read.

**A zone id is dropped.** `fe80::1%eth0` keys as `fe80::1`, so two hosts
reached over different interfaces share one key. Link-local addresses are
rarely a client identity, but it is worth knowing.

**Configuration fails loudly.** `TrustedProxies(["10.0.0.1/8"])` raises,
because it has host bits set. Some servers silently downgrade an entry
like that to a string literal that then matches nothing, leaving you with
an empty trusted set and no error.
