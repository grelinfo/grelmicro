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

!!! warning "The handler decorator goes on top"

    `@outbox.handler` registers the function it is handed and returns that
    same one. A decorator written below it wraps the module-level name,
    while the registry keeps holding the original, so it applies to direct
    calls and never to a delivered message. Put `@outbox.handler` on top.
    Written the other way round, the decorator below refuses with a
    `TypeError` that names the right order.

## Publishing to a broker

grelmicro has no publish/subscribe primitive and talks to no broker. Reach for [FastStream](https://faststream.airt.ai/), which covers Kafka, RabbitMQ, NATS, Redis, and MQTT. The two fit together in the handler:

```python
from faststream.kafka import KafkaBroker

broker = KafkaBroker("localhost:9092")


@outbox.handler(OrderPlaced)
async def emit_order_placed(message: Message[OrderPlaced]) -> None:
    await broker.publish(message.data, topic="orders", key=message.key)
```

The split is the point. The outbox makes the intent to publish durable, committed in the same transaction as the business write, so a crash can neither lose the event nor emit one for a rollback. FastStream carries the message to the broker. Neither half does the other's job on its own.

Both sides deliver at least once, so consumers downstream stay idempotent. `message.id` is stable across retries, which makes it the key to deduplicate on.

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
