# Consumer

A handler is an async function bound to a payload model or a topic string. Register as many as you like:

```python
@outbox.handler(WelcomeEmail)
async def send_welcome(message: Message[WelcomeEmail]) -> None:
    await mailer.send(to=message.data.to, idempotency_key=message.id)
```

`Message` carries everything the relay knows:

| field | meaning |
|---|---|
| `id` | stable message id, use it as the idempotency key for the side effect |
| `topic` | routing topic |
| `key` | ordering or partition key, `None` by default |
| `data` | the validated payload model, for typed handlers |
| `payload` | the raw payload dict, for topic handlers |
| `headers` | metadata and trace context |
| `attempts` | how many times delivery has been tried, starting at 1 |

Delivery is at least once, so a handler must be idempotent. See
[Delivery semantics](index.md#delivery-semantics).

## Controlling retries from the handler

Any exception retries the message with backoff. Raise `Retry` to reschedule on your own terms, or `Cancel` to dead-letter it now without burning the remaining attempts:

```python
from grelmicro.outbox import Cancel, Retry


@outbox.handler(ChargeCard)
async def charge(message: Message[ChargeCard]) -> None:
    result = await payments.charge(message.data, idempotency_key=message.id)
    if result.rate_limited:
        raise Retry(delay=timedelta(seconds=30))
    if result.card_declined:
        raise Cancel(reason="card declined")
```

## Retries and dead-letter

A failed delivery is retried with capped exponential backoff and full jitter. After `max_attempts` the message moves to the `dead` state with its last error recorded. It stops blocking the queue and is left for you to inspect. Redrive dead messages back to pending once the cause is fixed:

```python
await outbox.redrive(topic="email.welcome")
```

Alert on the oldest pending age and on any message entering the dead state.

Tune `max_attempts`, `retry_base`, `retry_max`, and `retry_jitter` on the
component, see [Configuration](relay.md#configuration).
