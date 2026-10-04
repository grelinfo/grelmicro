# Coming from Quarkus

SmallRye Health, MicroProfile Fault Tolerance, the scheduler, the cache and `quarkus-oidc` all have a grelmicro equivalent. The biggest difference: grelmicro keeps a circuit breaker's and a rate limit's state in Redis, PostgreSQL or SQLite, so every replica shares it. This page maps each concept, says where the behavior differs, and lists what grelmicro leaves to other libraries.

## Quick reference

| Quarkus | grelmicro | Page |
|---|---|---|
| `@Liveness`, `@Readiness`, `/q/health` | `@health.check(...)` with `/livez`, `/readyz` and `/healthz` | [Health](../health.md) |
| Extension health checks | `auto_health`, one check per provider, off by default | [Health](../health.md) |
| `@Retry` | `@retry(when=..., attempts=...)` | [Retry](../resilience/retry.md) |
| `@Timeout` | `Timeout(name, seconds=...)` | [Timeout](../resilience/timeout.md) |
| `@CircuitBreaker` | `CircuitBreaker(name)` | [Circuit Breaker](../resilience/circuit-breaker.md) |
| `@Bulkhead` | `Bulkhead(name, max_concurrent=...)` | [Bulkhead](../resilience/bulkhead.md) |
| `@Fallback` | `@fallback(when=..., default=...)` | [Fallback](../resilience/fallback.md) |
| SmallRye `@RateLimit` | `RateLimiter.sliding_window(...)`, `RateLimiter.token_bucket(...)` | [Rate Limiter](../resilience/rate-limiter.md) |
| Several fault tolerance annotations on one method | `Stack(...)` | [Composition](../resilience/composition.md) |
| `@Scheduled(every = ...)`, `@Scheduled(cron = ...)` | `@tasks.every(seconds=...)`, `@tasks.cron("...")` | [Task Scheduler](../task.md) |
| Quartz clustered mode | `gate="claim"` on a task | [Task Scheduler](../task.md#claim) |
| `@CacheResult`, `@CacheInvalidate` | `@cached(cache)`, `cache.delete(...)` | [@cached](../cache/cached.md) |
| Micrometer, `@Timed` | `Metrics()`, `@measure` | [Metrics](../metrics.md) |
| OpenTelemetry, `@WithSpan` | `Trace()`, `@instrument` | [Tracing](../tracing.md) |
| `quarkus-oidc` bearer tokens | `AuthenticatedRequests` with a `JWTVerifier` | [Authentication](../http/authentication.md) |
| `@PermissionsAllowed("orders_read")` | `Authenticated(scopes=["orders_read"])` | [Authentication](../http/authentication.md) |
| `quarkus-oidc-client` | `OAuthClient` with `ClientCredentials` | [Tokens](../security/tokens.md) |
| `@QuarkusTest`, `@InjectMock` | `TestClient(app)` with `micro.fake()`, `micro.override(...)` | [Testing](../testing.md) |

## Concept differences

### Shared state across replicas

A MicroProfile circuit breaker and a SmallRye rate limit keep their state in one JVM, per bean class and method. A grelmicro `CircuitBreaker` or `RateLimiter` keeps it in the backend, by name, so every replica sees the same breaker and spends the same budget. A grelmicro limiter also takes a `key=`, such as one budget per user.

### Retry counts calls

`@Retry(maxRetries = 4)` retries every `Exception` and makes up to 5 calls. `@retry(attempts=5)` makes up to 5 calls too, and `when=` is required: name the exceptions worth retrying.

### The circuit breaker counts consecutive failures

`@CircuitBreaker` opens on a failure ratio over the last `requestVolumeThreshold` calls. grelmicro opens after `error_threshold` consecutive failures, 5 by default, and closes after 2 successful probes.

### One worker per run without Quartz

`concurrentExecution = SKIP` only covers one instance, and running a job once across the cluster takes Quartz with a JDBC store. grelmicro's `gate="claim"` does it through Redis, PostgreSQL or SQLite, and claimed and local tasks sit side by side in one `Tasks`.

### Health has three fixed endpoints

`/livez` runs no check, `/readyz` runs the critical checks, and `/healthz` runs them all. There are no health groups and no startup endpoint. A check with `critical=False` stays out of readiness. A Quarkus liveness check, such as a deadlock detector, has no endpoint of its own: put it in `/healthz` with `critical=False`.

### Every route is protected by default

Quarkus endpoints are open unless annotated, or unless `deny-unannotated-endpoints` is set. Once `AuthenticatedRequests` is registered, grelmicro requires a token on every route. Open a route with `Anonymous()` or `exclude=`. grelmicro checks scopes. There are no roles: read the `groups` claim with `Claims` in the handler and answer `403` when you need one.

### The outbox needs no broker

The Debezium outbox extension writes the event in your transaction, and a CDC connector publishes it to a broker. grelmicro writes the message in your transaction too, then a relay in the same service hands it to your handler, with retries and a dead-letter state.

### Context lives on the call

MDC is a map for each thread. grelmicro context follows the call across `await`, and only `@instrument` and `span()` open it. Inside a route handler, open a `span(...)` before you call `add_context`.

## Side by side

### Fault tolerance on one call

=== "Quarkus"

    ```java
    @Retry(maxRetries = 2)
    @Timeout(1000)
    @CircuitBreaker(requestVolumeThreshold = 4)
    @Fallback(fallbackMethod = "fallbackRecommendations")
    public List<Coffee> recommendations(int id) {
        // ...
    }
    ```

=== "grelmicro"

    ```python
    --8<-- "resilience/stack.py"
    ```

### A job that runs on one node

=== "Quarkus"

    ```java
    @Transactional
    @Scheduled(every = "60s", identity = "task-job")
    void schedule() {
        Task task = new Task();
        task.persist();
    }
    ```

    ```properties
    quarkus.quartz.clustered=true
    quarkus.quartz.store-type=jdbc-cmt
    ```

=== "grelmicro"

    ```python
    --8<-- "task/interval_claim.py"
    ```

### A readiness check

=== "Quarkus"

    ```java
    @Readiness
    @ApplicationScoped
    public class DatabaseConnectionHealthCheck implements HealthCheck {

        @Override
        public HealthCheckResponse call() {
            return HealthCheckResponse.up("Database connection health check");
        }
    }
    ```

=== "grelmicro"

    ```python
    --8<-- "health/fastapi_app.py"
    ```

### A protected API

=== "Quarkus"

    ```java
    @PermissionsAllowed("orders_read")
    @GET
    @Path("/order")
    public List<Order> listOrders() {
        return List.of(new Order("1"));
    }
    ```

    ```properties
    quarkus.oidc.auth-server-url=http://localhost:8180/realms/quarkus
    quarkus.oidc.token.audience=orders-api
    ```

=== "grelmicro"

    ```python
    --8<-- "http/authentication.py"
    ```

## False friends

| Word | In Quarkus | In grelmicro |
|---|---|---|
| profile | A config profile such as `%prod.` | A [Shield](../resilience/shield.md) preset such as `internal` or `api`. `GREL_ENVIRONMENT` only names the deployment tier and selects no configuration |
| extension | A build-time module that generates code | No build step. Framework support lives in `grelmicro.integrations` and wires at runtime |
| Dev Services | Real services started in containers for dev and test | No containers. `micro.fake()` swaps grelmicro's backends to memory |
| bean | A container-managed object with injection | No beans. A component has a start and a stop |
| health groups | Endpoints for a named set of checks | No groups. `critical=False` and the `?exclude=` query are the levers |
| reactive | Mutiny and Vert.x | Plain `async` and `await` on asyncio |
| management interface | Health and metrics on port 9000 | `OpsServer`, for a process that serves no HTTP |

## No equivalent, do this instead

- **Config profiles**: keep one environment file per environment and load it with your runner. See [Configuration](../config.md).
- **Dev Services**: start containers with testcontainers-python in a pytest fixture, or use `micro.fake()` when the test is about your code.
- **Health groups and the startup probe**: use `critical=False`, `?exclude=`, and point `startupProbe` at `/livez`.
- **A failure-ratio circuit breaker**: grelmicro has the consecutive count only.
- **Roles with `@RolesAllowed`**: use scopes, or read the claim with `Claims` in the handler and answer `403`.
- **The Vert.x event bus**: call the function. Use the [Outbox](../outbox/index.md) for work after a commit, and FastStream for a broker.
- **Panache and Hibernate**: use SQLAlchemy or SQLModel. `Outbox.publish` takes their `AsyncSession`.
