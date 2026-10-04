# Coming from Spring Boot

Most of what Spring Boot, Actuator, ShedLock and Resilience4j give a service has a grelmicro equivalent. This page maps each one, says where the behavior differs, and lists what grelmicro leaves to other libraries.

## Quick reference

| Spring | grelmicro | Page |
|---|---|---|
| Actuator liveness and readiness probes | `HealthChecks` with `/livez`, `/readyz` and `/healthz` | [Health](../health.md) |
| A health indicator left out of readiness | `@health.check(..., critical=False)` | [Health](../health.md) |
| `management.server.port` | `OpsServer`, for a process that serves no HTTP | [Ops server](../http/server.md) |
| Micrometer, `/actuator/prometheus` | `Metrics()` and `metrics_router()` | [Metrics](../metrics.md) |
| `@Timed` | `@measure` | [Metrics](../metrics.md) |
| `@Scheduled(fixedDelay = ...)`, `@Scheduled(cron = ...)` | `@tasks.every(seconds=...)`, `@tasks.cron("...")` | [Task Scheduler](../task.md) |
| ShedLock `@SchedulerLock` | `gate="claim"` on a task | [Task Scheduler](../task.md#claim) |
| `@Cacheable`, `@CacheEvict` | `@cached(cache, tags=[...])`, `cache.delete_tags(...)` | [@cached](../cache/cached.md) |
| `@Cacheable(sync = true)` | `@cached(..., lock=True)` | [Stampede protection](../cache/cached.md#stampede-protection) |
| Resilience4j CircuitBreaker, Retry, RateLimiter, Bulkhead, TimeLimiter | `CircuitBreaker`, `@retry`, `RateLimiter`, `Bulkhead`, `Timeout` | [Resilience](../resilience/index.md) |
| SLF4J MDC | `@instrument`, `span()` and `add_context()` | [Tracing](../tracing.md) |
| OAuth2 resource server with JWT | `AuthenticatedRequests` with a `JWTVerifier` | [Authentication](../http/authentication.md) |
| `hasAuthority("SCOPE_x")` | `Authenticated(scopes=["x"])` | [Authentication](../http/authentication.md) |
| OAuth2 client credentials | `OAuthClient` with `ClientCredentials` | [Tokens](../security/tokens.md) |
| `@TransactionalEventListener` | `Outbox.publish(...)` and `@outbox.handler` | [Outbox](../outbox/index.md) |
| `@SpringBootTest` with MockMvc, `@MockitoBean` | `TestClient(app)` with `micro.fake()` | [Testing](../testing.md) |

## Concept differences

### A claimed task renews its lease

ShedLock always releases the lock after `lockAtMostFor`. A task that runs longer can run on a second node at the same time. A grelmicro task with `gate="claim"` renews its lease while the body runs, so `lease_duration` only bounds how long a crashed worker keeps the claim. A bare `async with TaskLock(...)` does not renew and behaves like ShedLock: `lockAtLeastFor` is `min_hold_duration` and `lockAtMostFor` is `lease_duration`.

A claimed cron task takes no lock at all. It records each fire it claims, and a fire missed while every worker was down runs once when a worker comes back.

### An interval waits for the previous run

`@tasks.every(seconds=60)` counts from the end of one run to the start of the next, like `fixedDelay`. There is no `fixedRate` equivalent: a run that takes 20 seconds starts the next one 80 seconds after it started.

### Health has three fixed endpoints, not groups

Actuator groups are named sets you define. grelmicro has three endpoints: `/livez` runs no check, `/readyz` runs the critical checks, `/healthz` runs them all. A check with `critical=False` stays out of readiness, and its failure keeps the answer at `200`. There is no custom group.

### Retry needs to know what to retry

Spring Framework 7 `@Retryable` retries every exception, three retries after the first call. grelmicro `@retry` requires `when=`, the exceptions worth retrying, and `attempts` counts every call, the first one included.

### The circuit breaker counts consecutive failures

Resilience4j opens on a failure rate or a slow-call rate over a sliding window. grelmicro opens after a number of consecutive failures. Its state can live in Redis, PostgreSQL or SQLite, so every replica sees the same breaker.

### Resilience runs in a fixed order

Resilience4j wraps a call as `Retry(CircuitBreaker(RateLimiter(TimeLimiter(Bulkhead(call)))))`. A grelmicro `Stack` runs `Fallback`, `Retry`, `CircuitBreaker`, `RateLimiter`, `Bulkhead` and then `Timeout`, so the timeout sits inside the bulkhead.

### Context lives on the call, not the thread

MDC is a map for each thread that you can write to anywhere. grelmicro context follows the call across `await`, and only `@instrument` and `span()` open it. Inside a route handler, wrap the work in `with span("checkout", user_id=...)` before you call `add_context`. Called outside a span, `add_context` does nothing.

### The outbox survives a crash

`@TransactionalEventListener` runs after the commit, in memory. A crash between the commit and the listener loses the event. `Outbox.publish(...)` writes the message in the same transaction, and a relay delivers it at least once, with retries and a dead-letter state.

## Side by side

### A job that runs on one node

=== "Spring Boot"

    ```java
    @Scheduled(fixedDelay = 60_000)
    @SchedulerLock(name = "cleanup", lockAtMostFor = "5m", lockAtLeastFor = "30s")
    public void cleanup() {
        // ...
    }
    ```

=== "grelmicro"

    ```python
    --8<-- "task/interval_claim.py"
    ```

### A check that stays out of readiness

=== "Spring Boot"

    ```yaml
    management:
      endpoint:
        health:
          group:
            readiness:
              include: "readinessState,db"
    ```

=== "grelmicro"

    ```python
    --8<-- "health/non_critical.py"
    ```

### A JWT resource server

=== "Spring Boot"

    ```java
    @Bean
    SecurityFilterChain api(HttpSecurity http) throws Exception {
        http
            .authorizeHttpRequests(auth -> auth
                .requestMatchers("/catalog").permitAll()
                .requestMatchers(HttpMethod.DELETE, "/orders/**")
                    .hasAuthority("SCOPE_orders:write")
                .anyRequest().authenticated())
            .oauth2ResourceServer(oauth2 -> oauth2.jwt(Customizer.withDefaults()));
        return http.build();
    }
    ```

=== "grelmicro"

    ```python
    --8<-- "http/authentication.py"
    ```

### A side effect after the commit

=== "Spring Boot"

    ```java
    @Transactional
    public void signUp(String email) {
        User user = users.save(new User(email));
        events.publishEvent(new UserSignedUp(user.getId(), email));
    }

    @TransactionalEventListener
    public void sendWelcome(UserSignedUp event) {
        // ...
    }
    ```

=== "grelmicro"

    ```python
    --8<-- "outbox/quickstart.py"
    ```

## False friends

| Word | In Spring | In grelmicro |
|---|---|---|
| profile | A named set of configuration, chosen by `spring.profiles.active` | A [Shield](../resilience/shield.md) preset such as `internal` or `api`. `GREL_ENVIRONMENT` only names the deployment tier and selects no configuration |
| container | The IoC container that builds and injects beans | The `Grelmicro` app that opens and closes your components, or a Docker container |
| component | Any bean found by `@Component` scanning | One pattern wired into the app, such as `Cache` or `Coordination`, with a start and a stop |
| provider | An `AuthenticationProvider` | A connection to one vendor, such as `RedisProvider` |
| tags | Micrometer meter dimensions | Cache tags for bulk invalidation. Metric dimensions are called attributes |

## No equivalent, do this instead

- **Profiles**: set the values for each environment in `GREL_*` variables or a mounted file. See [Configuration](../config.md).
- **Custom health groups**: use `critical=False` and the `?exclude=` query on `/readyz` and `/healthz`.
- **A failure-rate circuit breaker**: grelmicro has the consecutive count only.
- **An application event bus**: call the function. Use the [Outbox](../outbox/index.md) for work after a commit, and FastStream for a broker.
- **Spring Data**: use SQLAlchemy or SQLModel. `Outbox.publish` takes their `AsyncSession`.
- **WebSocket broadcast**: use your framework's WebSocket support with a broker. grelmicro still authenticates the handshake.
