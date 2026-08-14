# Idempotency

The `idempotency` module makes a retried operation safe to run more than once. It stores the response under a caller-supplied key. A repeated key within the configured lifetime replays the stored response instead of running the operation again.

This pairs with retries. Wrap a call in `Retry`, mark the operation idempotent, and a retry that lands after the first attempt already succeeded returns the stored response rather than charging the card twice.

- **[Idempotency](#the-block-form)**: an explicit block that runs the work once and replays it on repeat.
- **[@idempotent](#decorator)**: a decorator that derives the key from the call arguments.
- **[IdempotencyMiddleware](middleware.md)**: replays a whole HTTP response when a request repeats its `Idempotency-Key` header.

## Quick start

A FastAPI handler reads the key from the `Idempotency-Key` header and wraps the work in a block. Responses ride the cache layer, so one provider line covers the storage.

```python
from typing import Annotated

from fastapi import FastAPI, Header
from pydantic import BaseModel

from grelmicro import Grelmicro
from grelmicro.idempotency import Idempotency
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis])

app = FastAPI()
micro.install(app)


class ChargeResponse(BaseModel):
    id: str
    amount: int


idem = Idempotency[ChargeResponse]("charge", ttl=3600)


@app.post("/charge")
async def charge(
    amount: int,
    key: Annotated[str, Header(alias="Idempotency-Key")],
) -> ChargeResponse:
    async with idem(key) as op:
        if op.replayed:
            return op.result()
        response = await do_charge(amount)
        op.store(response)
        return response
```

The first request with a given key runs `do_charge` and stores the response. Any later request with the same key replays that response without charging again.

Redis needs the `redis` extra: `pip install "grelmicro[redis]"`. Tests swap the provider for the memory backend, see [Testing](../testing.md).

A replay has to find the stored response wherever the retry lands, so the `Cache` behind an `Idempotency` has to be shared by every replica. A `Cache` alone is happy on the memory backend, so name the `Cache` this one rides and the [backend check](../deployment.md#the-backend-check) holds you to it:

```python
micro = Grelmicro(uses=[Cache(redis, requires="cluster")])
```

To cover a whole app at once instead of one handler at a time, use
[IdempotencyMiddleware](middleware.md).

## The block form

`idem(key)` opens an async context manager. The yielded operation carries `replayed`, `result()`, and `store(...)`.

```python
async with idem(key) as op:
    if op.replayed:
        return op.result()
    response = await do_work()
    op.store(response)
    return response
```

On a first execution, `replayed` is `False`. Call `op.store(response)` to persist the response. On a replay, `replayed` is `True` and `op.result()` returns the stored value, typed as the stored type so the replay branch returns it without a cast. Calling `op.result()` on a first execution raises `IdempotencyStateError`, so guard it with `if op.replayed:`.

Exiting the block without calling `op.store(...)` on a first execution stores nothing. The operation opted out and a later call with the same key executes fresh.

## One-call form

`idem.run(key, factory)` owns the block. It runs the factory once, stores the response, and replays it on a repeated key. The factory can be sync or async. It mirrors `TTLCache.get_or_set`.

```python title="run.py"
--8<-- "idempotency/run.py"
```

The first call for a key runs the factory and stores its return value. A later call with the same key replays the stored value without running the factory. Pass `fingerprint=` to guard against a key reused with a different payload.

## Decorator

`@idempotent` derives the key from the call arguments and stores the return value. It mirrors `@cached`.

```python
from grelmicro.idempotency import idempotent


@idempotent(idem, key=lambda **kw: kw["idempotency_key"])
async def charge(*, amount: int, idempotency_key: str) -> ChargeResponse:
    return await do_charge(amount)
```

The first call for a key runs the function and stores its return value. A later call with the same key replays the stored value without running the function.

## Storage

Responses ride the cache layer. Pass an explicit `cache=` to bind a `TTLCache`, or leave it unset to resolve the active app's `Cache` component backend. Without either, the first call raises an out-of-context error.

```python
from grelmicro.cache import TTLCache
from grelmicro.idempotency import Idempotency

idem = Idempotency("charge", ttl=3600, cache=TTLCache(ttl=3600))
```

Responses serialize through the cache serializers. Name the response type and the store roundtrips it, so a replay returns the model itself:

```python
idem = Idempotency[ChargeResponse]("charge", ttl=3600)
```

Without a type parameter, responses store as JSON. Pass `serializer=ChargeResponse` where there is no parameter to read, or `serializer=PickleSerializer()` for a type Pydantic cannot adapt.

## Single-flight duplicates

A duplicate that arrives while the first execution is still in flight waits and receives the stored response. It folds across replicas when a Coordination lock backend is configured, and in-process otherwise.

```python
from grelmicro.coordination import Coordination
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[Cache(redis), Coordination(redis)])
```

With a lock backend, two replicas that receive the same key at the same time run the work once and both return the same response.

### Bounding the wait

The wait is unbounded by default. Pass `wait_timeout=` to bound it, on the block or on `run()`. Past it the wait raises `IdempotencyWaitTimeoutError`, which subclasses `TimeoutError`.

```python
from grelmicro.idempotency import IdempotencyWaitTimeoutError

try:
    async with idem(key, wait_timeout=5) as op:
        ...
except IdempotencyWaitTimeoutError:
    ...  # an execution for this key is still in flight
```

[IdempotencyMiddleware](middleware.md) bounds it at ten seconds and answers `409`.

## Payload fingerprint

Pass `fingerprint=` to guard against a key reused with a different payload. The fingerprint is a string the caller derives from the request body. It is stored on the first execution. A replay with a different fingerprint raises `IdempotencyConflictError`, because the same key with a different payload is a client bug.

```python
import hashlib

fingerprint = hashlib.sha256(raw_body).hexdigest()

async with idem(key, fingerprint=fingerprint) as op:
    ...
```

When no fingerprint is given, no check runs.

## What is and is not guaranteed

Guaranteed:

- A repeated key within `ttl` replays the stored response without running the operation again.
- An exception in the block or the decorated function stores nothing. A later retry with the same key executes fresh.
- A duplicate arriving mid-flight waits and replays the stored response.

Not guaranteed:

- A key replays only within `ttl`. After it elapses, the same key executes fresh.
- The work itself is not made atomic. Idempotency replays the response. Pair it with a transaction when the side effect must also be once-only.

## Configuration

Build with keyword arguments and tune `ttl` in deployment. Set
`GREL_IDEMPOTENCY_{NAME}_TTL` to change it without code changes (the default
instance drops the name segment and reads `GREL_IDEMPOTENCY_TTL`). The instance
reconfigures live from a mounted ConfigMap. See
[Live reconfiguration](../architecture/reconfigure.md).

!!! tip "Advanced"
    For the `from_config` declarative path and `pydantic-settings` composition,
    see [Declarative configuration](../advanced/config.md).
