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

## Split handlers across modules

An `OutboxRouter` groups the handlers of one feature. It needs no provider, so a feature module declares its handlers without importing the module that builds the app. The app then includes the router:

```python title="router.py"
--8<-- "outbox/router.py"
```

Output:
```text title="output"
billing.order_placed: charge order 42
```

Include a router in the constructor with `Outbox(postgres, routers=[...])`, or later with `outbox.include_router(router)`. A router includes other routers the same way, so a feature can split its own handlers further.

A router never changes a topic. The topic comes from the payload model or the topic string, so the producer publishes with `outbox.publish` and never needs to know which router handles the message. Set `__outbox_topic__` on the model to give it a namespaced topic such as `billing.order_placed`. A subclass inherits it, so set it again on a subclass that needs a topic of its own.

A router can be included in several outboxes, for example in each app an app factory builds. Including an `Outbox`, a router in itself, or a router an outbox already holds raises `OutboxRouterError`. Two handlers on the same topic raise `HandlerAlreadyRegisteredError` when the second one joins an outbox, and the error names both functions.

A router can be included and a handler added after the outbox started. The relay claims the new topic on its next poll. A router that is never included handles nothing, and its messages stay pending.

## Publishing to a broker

grelmicro has no publish/subscribe primitive and talks to no broker. Reach for [FastStream](https://faststream.airt.ai/), which covers Kafka, RabbitMQ, NATS, Redis, and MQTT. The two fit together in the handler:

```python
from faststream.kafka import KafkaBroker

from grelmicro.outbox import Message

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
from grelmicro.outbox import Cancel, Message, Retry


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

```python title="fragment"
await outbox.redrive(topic="email.welcome")
```

Alert on the oldest pending age and on any message entering the dead state.

Tune `max_attempts`, `retry_base`, `retry_max`, and `retry_jitter` on the
component, see [Configuration](relay.md#configuration).
