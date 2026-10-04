# Coming from Laravel

grelmicro is not a web framework and has no ORM. It runs inside FastAPI, Starlette, Litestar or FastStream. What it gives you maps to Laravel's atomic locks, cache, rate limiter, `onOneServer()` scheduler and after-commit jobs. This page maps each one, says where the behavior differs, and lists what to use for the rest.

## Quick reference

| Laravel | grelmicro | Page |
|---|---|---|
| `Schedule::call(...)->everyMinute()`, `->cron(...)` | `@tasks.every(seconds=...)`, `@tasks.cron("...")` | [Task Scheduler](../task.md) |
| `->onOneServer()` | `gate="claim"` on a task | [Task Scheduler](../task.md#claim) |
| `->withoutOverlapping()` | Nothing to add on one worker. `gate="claim"` across workers | [Task Scheduler](../task.md#interval-task) |
| `Cache::lock('name', 10)` | `Lock("name", lease_duration=10)` | [Lock](../coordination/lock.md) |
| `Cache::remember(...)` | `@cached(cache)`, `cache.get_or_set(...)` | [@cached](../cache/cached.md) |
| `Cache::flexible(...)` | `@cached(cache, early=0.1)`, a fraction of the TTL, not seconds | [Stampede protection](../cache/cached.md#stampede-protection) |
| `Cache::tags([...])->flush()` | `cache.delete_tags(...)` | [@cached](../cache/cached.md) |
| `RateLimiter::for(...)` and the `throttle` middleware | `RateLimitedRequests` for the app, `RateLimited` for one route | [Rate limit](../http/rate-limit.md) |
| `RateLimiter::attempt(...)` | `limiter.acquire(key=...)` | [Rate Limiter](../resilience/rate-limiter.md) |
| `->afterCommit()` on a queued job | `Outbox.publish(...)` inside the transaction | [Outbox](../outbox/index.md) |
| `failed_jobs`, `queue:retry` | The outbox dead-letter state, `outbox.redrive(...)` | [Outbox](../outbox/index.md) |
| Passport `auth:api` with `CheckToken::using(...)` | `AuthenticatedRequests` with a `JWTVerifier`, `Authenticated(scopes=[...])` | [Authentication](../http/authentication.md) |
| `Http::retry(3, 100)` | `@retry(when=httpx.HTTPError, attempts=3)` | [Retry](../resilience/retry.md) |
| `Log::withContext([...])` | `add_context()` inside a `span()` | [Tracing](../tracing.md) |
| The `/up` health route | `HealthChecks` with `/livez`, `/readyz` and `/healthz` | [Health](../health.md) |
| HTTP tests, `$this->get('/')` | Your framework's `TestClient` with `micro.fake()` | [Testing](../testing.md) |

## Concept differences

### The scheduler runs inside the service

Laravel runs `schedule:run` from a system cron entry every minute. grelmicro runs tasks in the service process, next to your routes, with no cron entry. A process that serves no HTTP gets its probes from an [`OpsServer`](../http/server.md).

### A claimed task renews its claim

`onOneServer()` takes an atomic lock for each run. A claimed interval task renews its lease while the body runs, so a slow run does not overlap the next interval on another server. If the backend stays unreachable for a whole lease, the claim is lost and another worker may run. Add a `Lock` as `sync` for work that must never overlap. A claimed cron task records each fire it claims, and runs a fire missed while every worker was down, once.

### A task never overlaps itself

In Laravel, a task runs even while the previous run is still going, unless you add `withoutOverlapping()`. A grelmicro task waits for its previous run on the same worker. Add `gate="claim"` to cover the other workers too.

### Locks wait unless you say otherwise

`Cache::lock(...)->get()` returns `false` at once when the lock is taken. `lock.acquire_nowait()` raises `WouldBlockError` instead. `async with lock:` waits like `block()`, and `lock.acquire(timeout=5)` gives up after five seconds with `LockTimeoutError`. A grelmicro lock is not reentrant: a second acquire from the same task raises `LockReentrantError`.

### Cache tags only drive invalidation

A Laravel tagged item can only be read with its tags. A grelmicro tag only marks the entry for `delete_tags`, and the key alone reads it.

### Context lives on the call

`Log::withContext([...])` adds fields to every later log line. grelmicro context follows the call across `await`, and only `@instrument` and `span()` open it. Inside a route handler, open a `span(...)` before you call `add_context`. Called outside a span, `add_context` does nothing.

### The outbox is a table in your transaction

`->afterCommit()` holds the dispatch until the transaction commits. `Outbox.publish(...)` writes the message in the same transaction as your rows, and a relay in the service delivers it at least once, with retries and a dead-letter state. The outbox needs PostgreSQL, through asyncpg or a SQLAlchemy `AsyncSession`.

### Tokens come from an identity provider

Sanctum and Passport issue tokens. grelmicro only verifies them: it checks a JWT that your identity provider signed. A Sanctum token is an opaque value stored in your database, so `JWTVerifier` cannot check it. Moving to grelmicro means moving token issuing to an identity provider such as Keycloak, Auth0, Entra ID or Cognito. Passport scopes and Sanctum abilities map to `Authenticated(scopes=[...])`, which requires all of them.

## Side by side

### A job that runs on one server

=== "Laravel"

    ```php
    use Illuminate\Support\Facades\Schedule;

    Schedule::command('report:generate')
        ->everyFiveMinutes()
        ->onOneServer();
    ```

=== "grelmicro"

    ```python
    --8<-- "task/cron_claim.py"
    ```

### A rate limit

=== "Laravel"

    ```php
    RateLimiter::for('api', function (Request $request) {
        return Limit::perMinute(60)->by($request->user()?->id ?: $request->ip());
    });
    ```

=== "grelmicro"

    ```python
    --8<-- "http/rate_limit.py"
    ```

Pass `key=` to `RateLimitedRequests` to count per user instead of per client address.

### Work after the commit

=== "Laravel"

    ```php
    ProcessPodcast::dispatch($podcast)->afterCommit();
    ```

=== "grelmicro"

    ```python
    --8<-- "outbox/quickstart.py"
    ```

### A protected API with scopes

=== "Laravel"

    ```php
    use Laravel\Passport\Http\Middleware\CheckToken;

    Route::get('/orders', function () {
        // Access token has both "orders:read" and "orders:create" scopes...
    })->middleware(['auth:api', CheckToken::using('orders:read', 'orders:create')]);
    ```

=== "grelmicro"

    ```python
    --8<-- "http/authentication.py"
    ```

## False friends

| Word | In Laravel | In grelmicro |
|---|---|---|
| guard | How users are authenticated, such as `session` or `sanctum` | Not an authentication term. Authentication is `AuthenticatedRequests` |
| provider | A service provider that boots the app, or a user provider | A connection to one vendor, such as `RedisProvider` |
| facade | A static interface to a service, such as `Cache::` | No such thing. You import and call the object |
| container | The service container for dependency injection | The `Grelmicro` app that opens and closes your components, or a Docker container |
| middleware groups | The `web` and `api` groups | No groups. A component adds its own ASGI middleware through `micro.install(app)` |
| tags | Cache tags that scope reads and flushes | Cache tags that only drive `delete_tags` |
| channels | Log channels, or broadcasting channels | Where a configuration warning shows up: as a Python warning and in the log |
| context | The `Context` facade, carried into queued jobs | The fields `add_context` adds inside a span. The trace context follows an outbox message, other fields do not |

## No equivalent, do this instead

- **Issuing tokens with Sanctum or Passport**: use an identity provider and `JWTVerifier.discover(...)`.
- **Cookie sessions for a single-page app**: keep them in your framework. grelmicro authenticates bearer tokens.
- **Gates and policies**: check ownership in the handler, with `CurrentPrincipal`. Use `check=` for rules about the caller.
- **A job queue, Horizon and batches**: use the [Outbox](../outbox/index.md) for work after a commit, and Celery, Dramatiq, taskiq or FastStream for a queue.
- **`Cache::funnel()` across servers**: `Bulkhead` limits concurrency in one process. Use a `Lock` for one at a time across replicas.
- **Pulse and Telescope**: send [Metrics](../metrics.md) to Prometheus and [Tracing](../tracing.md) to any OpenTelemetry backend.
- **Eloquent**: use SQLAlchemy or SQLModel. `Outbox.publish` takes their `AsyncSession`.
- **Artisan commands**: use Typer or Click. `python -m grelmicro check` checks your wiring in CI.
- **Broadcasting**: use your framework's WebSocket support with a broker. grelmicro still authenticates the handshake.
