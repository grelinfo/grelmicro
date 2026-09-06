# HTTP

grelmicro is not a web framework. These are the pieces that decide what your
service puts on the wire, whichever framework you picked.

| Page | What it covers |
|---|---|
| [Where a rule applies](where.md) | `include`, `exclude`, what wins, and how to read it back. Learn it once for all of them. |
| [Error Responses](errors.md) | Every rejection answered in one standard format, RFC 9457 or TM Forum. |
| [Conditional Requests](conditional.md) | `ETag` on reads, `If-Match` on writes, so a write cannot erase a change it never saw. |
| [Idempotency Middleware](idempotency.md) | A repeated `Idempotency-Key` replays the stored response instead of running the operation again. |
| [Response Cache](cache.md) | A repeated read is answered from the store instead of the handler, once per key across replicas. |
| [Rate Limit](rate-limit.md) | A caller over its budget is turned away at the edge, and told what it has left. |
| [Ops Server](server.md) | The health and metrics endpoints on a port of their own, for a process that serves no HTTP. |

The [Idempotency](../idempotency/index.md) page covers the same pattern away
from HTTP, as a block or a decorator around any operation.

One more HTTP piece lives with the concern it belongs to:
[Client IP](../security/clientip.md) resolves the real caller behind a reverse
proxy, which is a trust decision before it is an HTTP one.

Each of them is a component you register:

```python
micro = Grelmicro(
    uses=[
        ErrorResponses(),
        ConditionalRequests(),
        IdempotentRequests(),
    ]
)
micro.install(app)
```

Nothing happens without the component. grelmicro installs into a framework you
chose, so it changes nothing about how that framework answers until you ask.

Every one of them takes `include` and `exclude` to say which paths it acts on,
and every value on them is tunable from a mounted ConfigMap while the service
runs. [Where a rule applies](where.md) covers both, once, for all of them.

Every one of them is pure ASGI underneath, so it runs on FastAPI, Starlette,
and Litestar alike, and a framework that serves no HTTP simply skips it. Read
[Frameworks](../frameworks.md) for what that means.
