# Coming from Symfony

Symfony's Lock, RateLimiter, Cache, Scheduler, Messenger and security firewall all have a grelmicro equivalent. This page maps each one, says where the behavior differs, and lists what grelmicro leaves to other libraries.

## Quick reference

| Symfony | grelmicro | Page |
|---|---|---|
| Firewall with `stateless: true` and `access_token` | `AuthenticatedRequests`, with `exclude=` for public paths | [Authentication](../http/authentication.md) |
| `oidc` token handler with `audience`, `issuers`, `discovery` | `JWTVerifier.discover(...)` with `audience=` and `issuer=` | [JWT](../security/jwt.md) |
| `access_control`, `#[IsGranted]` | `Authenticated(scopes=[...])` | [Authentication](../http/authentication.md) |
| RateLimiter component | `RateLimiter.sliding_window(...)`, `RateLimiter.token_bucket(...)`, `RateLimitedRequests` | [Rate limit](../http/rate-limit.md) |
| Lock component, `createLock()`, `refresh()` | `Lock("name")`, `extend()` | [Lock](../coordination/lock.md) |
| Cache Contracts `get($key, fn)`, `TagAwareCacheInterface` | `@cached(cache, tags=[...])`, `cache.delete_tags(...)` | [@cached](../cache/cached.md) |
| Cache stampede lock, `beta` early expiration | `lock=` and `early=` on `@cached` | [Stampede protection](../cache/cached.md#stampede-protection) |
| `#[AsPeriodicTask]`, `#[AsCronTask]` | `@tasks.every(...)`, `@tasks.cron("...")` | [Task Scheduler](../task.md) |
| Scheduler `->lock(...)` | `gate=LeaderElection(...)` | [Task Scheduler](../task.md#run-on-one-worker) |
| Scheduler `->stateful()` with `processOnlyLastMissedRun(true)` | A claimed cron task | [Missed fires](../task.md#missed-fires) |
| Messenger retries and `failure_transport` | The outbox relay, its dead-letter state and `outbox.redrive(...)`. `@retry` for a call in the same process | [Outbox](../outbox/index.md), [Retry](../resilience/retry.md) |
| `WebTestCase`, `static::createClient()` | Your framework's `TestClient` with `micro.fake()` | [Testing](../testing.md) |

## Concept differences

### The scheduler runs inside the service

Symfony runs schedules in a `messenger:consume` worker. grelmicro runs them in the service process, next to your routes. A process that serves no HTTP gets its probes from an [`OpsServer`](../http/server.md).

### The schedule lock is a leader election

Symfony's `->lock()` covers the whole schedule: one worker runs it and the others wait as standbys. That is `gate=LeaderElection(...)`. `gate="claim"` is finer: each interval or fire goes to whichever worker claims it first.

### The outbox is a table, not a bus

Messenger routes messages to transports such as Doctrine, AMQP or Redis. The grelmicro outbox is one PostgreSQL table, written inside your transaction and drained by a relay in the same service. For a broker, use FastStream.

### Scopes, not roles

`Authenticated(scopes=[...])` checks the scopes in the token. There are no roles and no voters. Pass `check=` to add a check after the token verifies.

### Locks are not reentrant

A second acquire of the same `Lock` from the same task raises `LockReentrantError`. Use two instances when you need two independent locks.

## Side by side

### A job that runs on one worker

=== "Symfony"

    ```php
    #[AsSchedule('default')]
    class CleanupSchedule implements ScheduleProviderInterface
    {
        public function getSchedule(): Schedule
        {
            return new Schedule()
                ->add(RecurringMessage::every('1 minute', new Cleanup()))
                ->lock($this->lockFactory->createLock('cleanup'));
        }
    }
    ```

=== "grelmicro"

    ```python
    --8<-- "task/interval_leader.py"
    ```

### A protected API

=== "Symfony"

    ```yaml
    security:
        firewalls:
            main:
                stateless: true
                access_token:
                    token_handler:
                        oidc:
                            algorithms: ['RS256']
                            audience: 'orders-api'
                            issuers: ['https://auth.example.com/']
                            discovery:
                                base_uri: https://auth.example.com/
                                cache:
                                    id: cache.app
        access_control:
            - { path: ^/catalog, roles: PUBLIC_ACCESS }
            - { path: ^/, roles: IS_AUTHENTICATED }
    ```

=== "grelmicro"

    ```python
    --8<-- "http/authentication.py"
    ```

### A rate limit

=== "Symfony"

    ```yaml
    framework:
        rate_limiter:
            api:
                policy: 'sliding_window'
                limit: 100
                interval: '1 minute'
    ```

=== "grelmicro"

    ```python
    --8<-- "http/rate_limit.py"
    ```

### Work that must not be lost

=== "Symfony"

    ```yaml
    framework:
        messenger:
            failure_transport: failed
            transports:
                async:
                    dsn: '%env(MESSENGER_TRANSPORT_DSN)%'
                    retry_strategy:
                        max_retries: 3
                        delay: 1000
                        multiplier: 2
                failed: 'doctrine://default?queue_name=failed'
            routing:
                'App\Message\WelcomeEmail': async
    ```

=== "grelmicro"

    ```python
    --8<-- "outbox/quickstart.py"
    ```

## False friends

| Word | In Symfony | In grelmicro |
|---|---|---|
| container | The service container that injects dependencies | The `Grelmicro` app that opens and closes your components, or a Docker container |
| component | A reusable PHP library, such as Lock | One pattern wired into the app, such as `Cache` or `Coordination`, with a start and a stop |
| provider | A user provider that loads users | A connection to one vendor, such as `RedisProvider` |
| profile | The profiler's record of one request | A [Shield](../resilience/shield.md) preset such as `internal` or `api` |
| tags | Service tags. Cache tags mean the same in both | Cache tags for bulk invalidation |
| middleware | Messenger bus middleware | ASGI middleware a component adds through `micro.install(app)` |
| channels | Monolog channels such as `app` or `doctrine` | Where a configuration warning shows up: as a Python warning and in the log |

## No equivalent, do this instead

- **Voters**: check ownership in the handler, with `CurrentPrincipal`.
- **Console commands**: use argparse, Typer or Click. `python -m grelmicro check` checks your wiring in CI.
- **The profiler**: use request spans from [Tracing](../tracing.md) and [Metrics](../metrics.md).
- **EventDispatcher**: call the function. Use the [Outbox](../outbox/index.md) for work after a commit.
- **Doctrine**: use SQLAlchemy or SQLModel. `Outbox.publish` takes their `AsyncSession`.
- **Mercure**: use your framework's WebSocket support with a broker. grelmicro still authenticates the handshake.
- **A fixed-window rate limiter**: grelmicro has sliding window and token bucket.
