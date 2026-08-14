# Changelog

## 0.37.3 - 2026-08-14

### Added

* ✨ Build the cache serializer from the type parameter. `TTLCache[User](ttl=300)` serializes with `PydanticSerializer(User)`, so the model is named once instead of twice. `TTLCache()` and `TTLCache[bytes]()` still store raw bytes, and so does a type parameter Pydantic cannot adapt. ([#684](https://github.com/grelinfo/grelmicro/issues/684))
* ✨ Accept a type wherever a serializer is accepted. `micro.cache.ttl(ttl=300, serializer=User)` and `Idempotency("http", serializer=Response)` build the `PydanticSerializer`, which is the way in for a factory that has no type parameter to read. ([#684](https://github.com/grelinfo/grelmicro/issues/684))

### Changed

* ✨ `Idempotency[Response]` stores responses with `PydanticSerializer(Response)` and replays the model itself. It used to store JSON, which cannot even encode a Pydantic model, so the typed form raised on the first response it was given. `Idempotency("http")` without a type parameter still stores JSON. ([#684](https://github.com/grelinfo/grelmicro/issues/684))

### Docs

* 📝 Return a typed value in every example. The README, the guides, the snippets, and the demo app returned `dict`, which read as untyped scripting rather than a type-safe library. Each one now returns a Pydantic model, a plain `str`, or an `int`, whichever is shortest for the point it makes. ([#679](https://github.com/grelinfo/grelmicro/issues/679))

## 0.37.2 - 2026-08-13

### Changed

* 📝 Split the six longest guide pages so each one answers a single question. Cache, Idempotency, Coordination, Outbox, Logging, and Providers are now sections with an index page and one page per topic. The top-level URLs are unchanged. ([#681](https://github.com/grelinfo/grelmicro/issues/681))
* 📝 Move Redis connection settings off the cache page and onto [Redis and Valkey](providers/redis.md). A pattern page says which backends work and links out. ([#681](https://github.com/grelinfo/grelmicro/issues/681))
* 📝 Move the logging benchmark table to [Benchmarks](benchmarks.md), next to every other measurement. ([#681](https://github.com/grelinfo/grelmicro/issues/681))

### Fixed

* 📝 Fail the docs build on a link whose anchor no longer exists. `mkdocs build --strict` caught a missing file but let a moved heading through. ([#681](https://github.com/grelinfo/grelmicro/issues/681))
* 📝 Render the ten snippets that no page included, and delete five that nothing needed. A test now fails on a snippet no page renders. ([#681](https://github.com/grelinfo/grelmicro/issues/681))

## 0.37.1 - 2026-08-13

### Added

* ✨ Add `ValkeyConfig`, which accepts Valkey's own URL schemes. `ValkeyProvider` reads `valkey://` in the constructor and in `VALKEY_URL`, but `from_config` took a `RedisConfig`, which refuses those schemes, so the one path that could not name the server was the config object. `from_config` still takes a `RedisConfig`. ([#718](https://github.com/grelinfo/grelmicro/issues/718))

### Fixed

* 🐛 Validate a URL passed to a provider exactly as one read from the environment. `RedisProvider("anything://host")` handed the string to redis-py and failed with its `ValueError`, while `REDIS_URL` was checked against the provider's own URL type and raised `RedisProviderConfigError`. Both paths now validate against one type, so they accept the same URLs and fail the same way, and `PostgresProvider` does too. A URL that a client library used to accept and the URL type refuses, such as a host-less authority, now raises at construction. ([#718](https://github.com/grelinfo/grelmicro/issues/718))
* 🐛 Carry the Sentinel password through `ValkeyProvider.from_config`. It was dropped, so a Valkey Sentinel built from a config connected to the Sentinel servers unauthenticated while the same config on `RedisProvider` authenticated. ([#718](https://github.com/grelinfo/grelmicro/issues/718))

## 0.37.0 - 2026-08-11

### Breaking

* 💥 Settle on **Component** as the one word for app-level wiring. `RateLimiterRegistry` becomes `RateLimiterComponent` and `CircuitBreakerRegistry` becomes `CircuitBreakerComponent`. Neither ever registered anything: each wraps one backend, so the name borrowed a contract it did not honor. Update `uses=[...]` and imports. ([#682](https://github.com/grelinfo/grelmicro/issues/682))
* 💥 `health_router(registry=...)` is now `health_router(component=...)`, matching `metrics_router(component=...)`. ([#682](https://github.com/grelinfo/grelmicro/issues/682))

### Added

* ✨ Refuse to start when a backend cannot keep the promise its component makes. Declare the tier with `GREL_ENVIRONMENT` or `Grelmicro(environment=...)`, and in `production` or `staging` a `Coordination` or `Outbox` bound to a memory or SQLite backend raises `BackendScopeError` before the first connection opens. A lock that excludes nothing the moment a second replica starts used to say nothing at all. ([#683](https://github.com/grelinfo/grelmicro/issues/683))
* ✨ Add `scope` to every adapter and `requires=` to every component that holds a backend. A backend provides a scope (`process`, `host` or `cluster`), a component requires one, and `Coordination(memory, requires="process")` declares a single-process deployment instead of muting a check. `RateLimiterComponent(redis, requires="cluster")` reads the other way and fails the day someone points it at memory. ([#683](https://github.com/grelinfo/grelmicro/issues/683))
* ✨ Report the same finding as a warning on two channels when no tier is declared, and stay silent in `development` and `test`. A value naming no tier, such as `preprod`, warns and reads as undeclared, so a fleet with its own tier names keeps booting and `prodution` is loud instead of silent. ([#683](https://github.com/grelinfo/grelmicro/issues/683))
* ✨ Add `micro.check_backends()`, which answers for production from a process that declares something else, so a test catches the wiring before a pod does. It raises `BackendScopeError` naming every binding that does not hold. ([#683](https://github.com/grelinfo/grelmicro/issues/683))
* ✨ Set the OpenTelemetry `deployment.environment.name` resource attribute from the declared tier, so one variable gates the check and names the environment in every trace. ([#683](https://github.com/grelinfo/grelmicro/issues/683))
* ✨ Read Valkey's own URL schemes wherever a Redis scheme works. `valkey://`, `valkeys://`, `valkey+sentinel://` and `valkey+cluster://` are accepted by `ValkeyProvider` in the constructor and in `VALKEY_URL` alike, so a deployment can name the server it runs. The URL keeps the scheme you wrote, so logs and errors name that server too. `RedisProvider` keeps the `redis` schemes only. ([#716](https://github.com/grelinfo/grelmicro/issues/716))

### Fixed

* 🐛 Register the read-write lock adapters under an entry-point group, like every other component kind. Without one, `readwritelock` short names resolved to nothing and a third-party adapter had nowhere to register. ([#714](https://github.com/grelinfo/grelmicro/issues/714))
* 🐛 Add `MemoryProvider.outbox()`. The memory outbox adapter ships and the capability matrix lists it, but the provider had no factory for it, so `Outbox(MemoryProvider())` raised while every other kind resolved. The SQL staging settings (`table`, `auto_migrate`, `notify`) are accepted and ignored, since messages live in a dict. ([#714](https://github.com/grelinfo/grelmicro/issues/714))
* 🐛 Wrap a bare read-write lock or schedule backend into its Component. `uses=[RedisReadWriteLockAdapter()]` registered no Component at all, so the backend was lifecycled, resolved by nothing, and the pattern failed on first use. Every coordination backend now wraps into the slot it belongs to. ([#712](https://github.com/grelinfo/grelmicro/issues/712))
* 🐛 Wire the read-write lock from a bare Provider. `Grelmicro(uses=[redis])` registered a `Coordination` holding the lock, election and schedule backends and left the read-write lock unset, so `ReadWriteLock` failed on first use with a message telling you to pass the provider you had already passed. The default Component now wires every coordination backend, and one list drives the wiring, the Provider discovery and the backend scope check, so the next backend added reaches all three. ([#710](https://github.com/grelinfo/grelmicro/issues/710))
* 🐛 Adopt a Provider that only a read-write lock backend borrows. `Grelmicro` walks a `Coordination` to find the Providers its backends borrow, and the read-write lock was missing from that walk, so `Coordination(rwlock=...)` with a Provider left out of `uses=` started clean and then raised `OutOfContextError` on first use. Pool sharing and open-ordering missed it the same way. ([#707](https://github.com/grelinfo/grelmicro/issues/707))
* 🐛 Cover uvicorn's takeover on every logging backend. `stdlib`, `structlog`, and `loguru` all hand uvicorn's own loggers the matching formatter, so one process emits one format whichever backend runs the app, and each is now tested. ([#705](https://github.com/grelinfo/grelmicro/issues/705))
* 🐛 Read a record as a request only when it is one. `UvicornAccessFormatter` split any record carrying five or more positional arguments, so an application record reaching it through a shared handler was rendered as a request line and lost its message. It now splits a record that carries uvicorn's access message, or that comes from uvicorn's access logger, and formats anything else whole. ([#705](https://github.com/grelinfo/grelmicro/issues/705))
* 🐛 Keep a log record readable after uvicorn's access formatter has seen it. `UvicornAccessFormatter` split the request fields by rewriting `msg` and `args` on the record itself, so any record carrying five or more positional arguments reached every later reader as `"%s %s %s"`: a second handler on the same logger, a queue listener, or a test reading `caplog`. The split now runs on a copy. ([#705](https://github.com/grelinfo/grelmicro/issues/705))

### Docs

* 📝 Say that a bare Provider registers every kind it serves except `outbox`, which carries handlers and a relay and is built where those are declared. Four docstrings and the wiring guide claimed every kind. ([#710](https://github.com/grelinfo/grelmicro/issues/710))
* 📝 Open every feature example on a real backend. [First Steps](first-steps.md), [Cache](cache/index.md), [Idempotency](idempotency/index.md), and [Coordination](coordination/index.md) all started on the memory provider with a note that it runs as-is, so the memory backend read as the normal choice. It is not: a distributed lock on memory gives no mutual exclusion the moment a second replica starts. Each page now starts on Redis and names the extra to install. [First Steps](first-steps.md) adds the one command that runs it. ([#680](https://github.com/grelinfo/grelmicro/issues/680))
* 📝 Keep memory where it belongs and say why it is there. It stays on the [testing](testing.md) page, in the backend tabs, in the provider reference, and in the landing example that is about a process-local rate limiter. The cache backend tabs led with Memory and now lead with Redis. ([#680](https://github.com/grelinfo/grelmicro/issues/680))
* 📝 Show the shortest correct wiring. `Grelmicro(uses=[redis])` registers a Component for every kind the Provider serves, and a bare backend is wrapped for you, so the landing page, the providers guide, and the resilience snippets no longer name a Component to do what the Provider already does. The Component classes now appear only where they are needed: a second named instance and `micro.override(...)`. ([#682](https://github.com/grelinfo/grelmicro/issues/682))
* 📝 Drop "Registry" from the mental model. [First Steps](first-steps.md) offered "Component or Registry" as one bullet with two names, and the word lingered in the health, rate limiter, and architecture pages. ([#682](https://github.com/grelinfo/grelmicro/issues/682))
* 📝 Fix the `Component` protocol docstring. It illustrated the protocol with a `Tasks` class carrying `kind = "task"`, but `Tasks` is a plain async context manager and no such kind exists. `Grelmicro.get` listed that kind too. ([#682](https://github.com/grelinfo/grelmicro/issues/682))
* 📝 Rewrite the [roadmap](roadmap.md) as direction instead of a feature list. It was a list of named features under **Next** and **Later**, which is what the issue tracker already is, so it went stale the day anything shipped: it still offered the FastAPI `Idempotency-Key` integration, the FastStream integration, and the transactional outbox as future work, all three of which ship. It now describes seven directions, names a few concretes under each as illustration, and points at the tracker once for the queue. Nothing is duplicated, so nothing falls out of sync. ([#647](https://github.com/grelinfo/grelmicro/issues/647))
* 📝 Stop framing the roadmap around a 1.0 that is not on the calendar. It opened with "post-1.0 items" while the project runs on `0.x`, and split into **Next** and **Later**, which read as release buckets without being any. Direction has no buckets, so both are gone. ([#647](https://github.com/grelinfo/grelmicro/issues/647))
* 📝 Correct the rate limit header standard. The docs cited RFC 9211, which defines `Cache-Status` and has nothing to do with rate limits. The IETF fields are an [Internet-Draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/) that now defines `RateLimit` and `RateLimit-Policy` with `q`, `r`, and `t` parameters, not the `RateLimit-Limit` / `-Remaining` / `-Reset` names the [Rate Limiter](resilience/rate-limiter.md#result-fields) table showed. The table matches the draft and shows a real response. ([#647](https://github.com/grelinfo/grelmicro/issues/647))
* 📝 Match the README module table to what ships. Resilience listed two patterns out of seven, Cache and Coordination did not name Valkey, Cache named an adapter class instead of its backends, and Client IP was missing. The "why" line now names idempotency and metrics too. ([#647](https://github.com/grelinfo/grelmicro/issues/647))
* 📝 Keep the first README example to one idea. It ended by naming a container and a registry in the section meant to prove how little grelmicro asks for. That sentence is now a link to the example that introduces them. ([#647](https://github.com/grelinfo/grelmicro/issues/647))
* 📝 Stop [Shield](resilience/shield.md#what-shield-does-not-do) contradicting the roadmap. Hedged requests were called "not on the roadmap" while the roadmap listed them, fleet-wide retry budgets and deadline propagation read as never rather than planned, and one line said "async-only in 1.0" on a project that ships `0.x`. ([#647](https://github.com/grelinfo/grelmicro/issues/647))
* 📝 Say once, in the [documentation conventions](https://github.com/grelinfo/grelmicro/blob/main/CONTRIBUTING.md#documentation), that the roadmap holds direction and the issue tracker holds the queue. It is not in the per-pull-request checklist, because a page you touch when direction changes is not a page you check on every change. ([#647](https://github.com/grelinfo/grelmicro/issues/647))
* 📝 Make the idempotency quick start actually run. It built a `Grelmicro` app and never installed it, so the handler raised `OutOfContextError` on the first request while the page said it runs as-is. It calls `micro.install(app)` now. ([#647](https://github.com/grelinfo/grelmicro/issues/647))
* 📝 Stop claiming `RateLimitResult` carries everything an IETF rate limit header needs. It carries the per-request values. `RateLimit-Policy` describes the policy, so its window comes from the config you built the limiter with, and the [Rate Limiter](resilience/rate-limiter.md#result-fields) page now says where to read it. ([#647](https://github.com/grelinfo/grelmicro/issues/647))
* 📝 Fix the stale links and lists a reader trips over: the capability matrix pointed at a closed roadmap issue, said Memory adapters take no Provider when `MemoryProvider` ships, and listed three Providers out of five. The architecture index omitted three of its own pages, the ConfigMap page omitted YAML and TOML, the task page never mentioned cron in its own summary, and the idempotency quick start still opened with `Cache(MemoryCacheAdapter())`. ([#647](https://github.com/grelinfo/grelmicro/issues/647))

## 0.36.0 - 2026-08-09

### Breaking

* 💥 Reject a timezone abbreviation that names no zone. `GREL_LOG_TIMEZONE=CEST` used to validate and then fail at startup when `zoneinfo` could not load it. Names such as `CEST`, `PST`, `PDT`, `EDT`, `BST`, and `JST` are DST variants, not zones, and pinning one would freeze the offset year-round. Use the zone: `Europe/Zurich`, not `CEST`. Real zone names that look like abbreviations, such as `CET`, `EET`, `GMT`, `EST`, `MST`, and `HST`, keep working. ([#645](https://github.com/grelinfo/grelmicro/issues/645))
* 💥 Remove `LogTimeZoneType`. Use [`TimeZoneName`](reference/types.md) from `grelmicro.types`, which every component that takes a timezone now shares. ([#645](https://github.com/grelinfo/grelmicro/issues/645))
* 💥 Starting `Tasks` twice raises `TaskStartOperationError` instead of `TaskAddOperationError`, whose message advised calling `add_task` earlier and did not describe the mistake. ([#645](https://github.com/grelinfo/grelmicro/issues/645))
* 💥 A negative `Tasks(shutdown_timeout=...)` raises `TaskSettingsValidationError` instead of a plain `ValueError`, matching every other component. It still subclasses `ValueError`. ([#645](https://github.com/grelinfo/grelmicro/issues/645))

### Added

* ✨ Configure the task timezone once with [`Tasks(timezone=...)`](task.md#timezone), instead of repeating it on every cron task. A `TaskRouter` takes the timezone of the `Tasks` that includes it, in whatever order the wiring happens, and `TaskRouter(timezone=...)` gives one group of tasks a different clock. Nearest declaration wins: the task, then its router, then the `Tasks`. A cron task now reports its `timezone` for introspection. ([#645](https://github.com/grelinfo/grelmicro/issues/645))
* ✨ Add [`GREL_TIMEZONE`](config.md#one-timezone-for-the-whole-service), one variable saying what wall clock the service runs on. `Tasks` and `Log` both read it, and a component variable still wins, so `GREL_LOG_TIMEZONE=UTC` keeps logs on UTC under a service that schedules on local time. grelmicro ignores the POSIX `TZ` variable on purpose, since `TZ` falls back to UTC without complaint on a value it cannot parse. ([#645](https://github.com/grelinfo/grelmicro/issues/645))
* ✨ Add `TasksConfig` and `Tasks.from_config(...)`, so tasks configure like every other component. `timezone` and `shutdown_timeout` resolve from `GREL_TASK_*`. `Tasks` supports live reconfiguration of `shutdown_timeout`. `timezone` is startup-only, and an attempt to change it from a mounted ConfigMap is reported rather than applied. ([#645](https://github.com/grelinfo/grelmicro/issues/645))
* ✨ Log timestamps carry their UTC offset in the `TEXT` and `PRETTY` formats, rendered as `Z` for UTC. Without it, a non-UTC log timezone made the repeated hour after a daylight saving transition read as though time ran backwards. `JSON` and `LOGFMT` already carried the offset. ([#645](https://github.com/grelinfo/grelmicro/issues/645))
* ✨ Add [`ReadWriteLock`](coordination/read-write-lock.md), a distributed lock that lets every reader in at once and keeps writers alone. `lock.read` and `lock.write` are two views of one lock, each with `acquire`, `acquire_nowait`, `extend`, `release`, and a `from_thread` adapter. It is writer-preferring: a writer that finds readers in the way records an intent, so readers arriving after it wait and writers never starve. Every holder has its own lease, so a reader that died is reaped by the next acquire instead of blocking a writer until a shared expiry fires. `ReadGuard` and `WriteGuard` are distinct types, so a function that writes can demand the write guard in its signature, and reading a token from a spent guard raises `LockNotOwnedError` instead of handing back a stale one. `WriteGuard.poisoned` says the previous writer's lease expired without a release, and `await guard.downgrade()` turns a write lease into a read lease with no gap for another writer. Upgrading raises `LockUpgradeError` rather than shipping a deadlock. Redis, Valkey, PostgreSQL, SQLite, Kubernetes, and Memory all ship an adapter and pass one shared conformance suite. ([#686](https://github.com/grelinfo/grelmicro/issues/686))

### Fixed

* 🐛 Import `grelmicro.log` on an image with no timezone database. `pydantic-extra-types` read the whole timezone database while defining its type, so on a distroless or scratch image the import raised `ImportError` before any timezone was configured, and an app that never touched a timezone could not start. grelmicro now validates timezone names itself, and the default `UTC` needs no timezone database at all. Any other name reports what to install. This also drops the `pydantic-extra-types` dependency. ([#645](https://github.com/grelinfo/grelmicro/issues/645))
* 🐛 Fire a cron task once when the clocks go back. A wall time the clock passes twice resolved to the second pass, which sits above the durable last-fire state, so a `30 2 * * *` task claimed the fire again and ran a second time. An ambiguous time now always resolves to its first occurrence. ([#645](https://github.com/grelinfo/grelmicro/issues/645))
* 🐛 Stop a cron task spinning through the repeated hour when the clocks go back. Inside that hour the next matching minute resolves to an instant already past, so the loop woke immediately and read the schedule backend again, for up to an hour on every worker. It now waits for the next minute instead. ([#645](https://github.com/grelinfo/grelmicro/issues/645))

## 0.35.2 - 2026-08-06

### Fixed

* 🐛 Log the ignored-variable report, so it survives a JSON log stream. A `GREL_*` variable set without `GREL_ENV_LOAD` was reported through `warnings` only, which writes plain text to stderr, so in a pod the one line explaining why `GREL_LOG_LEVEL=DEBUG` did nothing was the one line the log collector could not parse. The report now also goes to the `grelmicro` logger, which the default backend renders like every other record, with the name in a `variable` field an alert can match. A component resolves its config before logging exists, so a report made then waits and goes out as soon as logging is configured. `Log` restores the reporting state on exit along with the handlers it replaced, so a second lifecycle formats its own reports. The `GrelmicroConfigWarning` channel is unchanged. ([#676](https://github.com/grelinfo/grelmicro/issues/676))

### Docs

* 📝 Add a [Deployment](deployment.md) guide, which says `GREL_ENV_LOAD=1` out loud and puts it in the image rather than the manifest, where one copy always forgets it. Covers the log format and the probe noise, the probe endpoints, the shutdown window against `Tasks(shutdown_timeout=...)`, and a Deployment manifest that applies as it stands. ([#676](https://github.com/grelinfo/grelmicro/issues/676))
* 📝 Scope the resolution order to the `GREL_*` namespace, in [Configuration](config.md#how-a-value-is-resolved). Step 2 read as though `GREL_ENV_LOAD` gated every environment variable, which no Provider has ever obeyed: `RedisProvider()` reads `REDIS_URL` with no flag, since that name belongs to the deployment rather than to grelmicro, and a missing one fails at construction naming the variable it wanted instead of falling back to a default. [Providers](providers/index.md) says the same where a reader of the env-driven recipe will see it. ([#676](https://github.com/grelinfo/grelmicro/issues/676))

## 0.35.1 - 2026-08-06

### Upgrading

**Uvicorn's own log lines change format.** `configure()` now applies your format to uvicorn's loggers, so lines that used to look like this:

```
INFO:     127.0.0.1:54321 - "POST /orders HTTP/1.1" 200 OK
```

now match everything else your app emits, with a timestamp, a level field, and structured request fields. That is the point, but a pipeline parsing uvicorn's plain format needs updating, or the old behaviour back:

```python
configure(uvicorn_enabled=False)
```

**A misconfigured `GREL_*` variable now warns.** Setting one without `GREL_ENV_LOAD` used to pass silently and now raises `GrelmicroConfigWarning`. A suite running `-W error`, or pytest with `filterwarnings = error`, will fail on it.

Fix the configuration, which is the point of the warning:

```bash
GREL_ENV_LOAD=1          # read GREL_* variables
```

or pass the value directly, which never needs the flag:

```python
configure(format="PRETTY")
```

To keep the warning visible without failing a build, filter the category rather than the message:

```toml
filterwarnings = ["error", "ignore::grelmicro.GrelmicroConfigWarning"]
```

### Features

* ✨ Configure the Sentinel password from the environment with `<prefix>SENTINEL_PASSWORD`. Sentinel servers commonly run with their own `requirepass`, and the Bitnami Redis chart enables it by default, but nothing read that password so the Sentinel connections went unauthenticated while the data connections worked. Only `RedisProvider.sentinel(...)` carried it, and that factory takes host and port pairs rather than a URL, so an authenticated Sentinel could not be expressed as a URL at all. It applies only when set, never inferred from the data password, because `AUTH` against a server without `requirepass` fails and would break every unauthenticated deployment. Set alongside a non-Sentinel scheme it warns rather than passing silently. Also available as `sentinel_password=` on the constructor and on `RedisConfig`. ([#661](https://github.com/grelinfo/grelmicro/issues/661))
* ✨ Make uvicorn's own logs match the application format, with no log config file. Uvicorn installs its own handlers and turns propagation off, so its lines never reached the handler `configure()` sets up and one process emitted two formats, the uvicorn half carrying no timestamp, no level field and no trace context. `configure()` now reformats them. Its handlers are kept and only the formatter is replaced, so the stderr/stdout split survives and access lines keep their structured fields. Pass `uvicorn_enabled=False`, or set `GREL_LOG_UVICORN_ENABLED=false`, when uvicorn's logging is configured elsewhere such as with `--log-config`. ([#666](https://github.com/grelinfo/grelmicro/issues/666))
* ✨ Quiet health probe access logs with `ProbeFilter`. Attach it to the access logger the same way as `DuplicateFilter` and `RateLimitFilter`. Kubernetes polls `/livez`, `/readyz` and `/healthz` every few seconds forever, and the access log reported each one, so a healthy pod logged almost nothing else. Suppressing them took a `logging.Filter` that reflected on the shape of uvicorn's access record. Only responses below `400` are dropped, so a readiness check that starts refusing traffic still appears. Paths match by suffix, so `health_router(prefix=...)` needs no configuration, and `paths=` covers other polled endpoints. ([#667](https://github.com/grelinfo/grelmicro/issues/667))

### Fixed

* 🐛 Keep the log line when a field cannot be serialized. An `extra={"url": httpx.URL(...)}` raised `TypeError` out of the formatter, so one unusual value destroyed the record and every field beside it. A value JSON has no representation for is now written as its `repr`, on both the stdlib and orjson paths. Elsewhere serialization still raises, because a cache value that cannot round-trip is a real error. ([#666](https://github.com/grelinfo/grelmicro/issues/666))
* 🐛 Set `record.message` when formatting, as `logging.Formatter` does. These formatters build their own mapping instead of calling up, so anything reading `record.message` afterwards saw an attribute that was never set, including pytest's `caplog` and any handler that formats a record twice. ([#666](https://github.com/grelinfo/grelmicro/issues/666))
* 🐛 Open a Provider before the Component that borrows it, whatever order they are listed in. A Provider left out of `uses=` was already discovered and inserted ahead of its Component, but one listed *after* it only got a warning and then failed on startup with `OutOfContextError`, so listing a Provider was worse than omitting it. Both cases are now reordered the same way: `uses=` says what the app is made of, and grelmicro opens it in dependency order. `Grelmicro(strict=True)` still raises `LifecycleOrderError`, for callers who want the list they wrote to be the list that runs. ([#665](https://github.com/grelinfo/grelmicro/issues/665))
* 🐛 Say so when a `GREL_*` variable is set but not applied. Environment-driven configuration is opt-in behind `GREL_ENV_LOAD`, so a documented variable such as `GREL_LOG_FORMAT` was read by nobody and the default applied with nothing reported. It now raises `GrelmicroConfigWarning` once, naming the variable and the flag. It is its own category so it can be filtered precisely, without silencing every `UserWarning` and without matching on message text, the way pytest ships `PytestConfigWarning`. Only the exact names a config declares are matched, never the prefix, because Kubernetes injects `{SVCNAME}_SERVICE_HOST` for every Service and a prefix sweep would warn on every pod start. An explicit `env_load=False` is a decision and stays silent. ([#662](https://github.com/grelinfo/grelmicro/issues/662))

### Docs

* 📝 Teach how a value is resolved, in [Configuration](config.md#how-a-value-is-resolved). Keyword arguments, environment behind `GREL_ENV_LOAD`, and a file through `ExternalConfig`, with a local development recipe that does not need exported variables. Says plainly that `ExternalConfig` reconfigures live components and not `Log`, so log format in local development comes from `configure(...)` or a loaded `.env`. ([#662](https://github.com/grelinfo/grelmicro/issues/662))
* 📝 Say why orjson is not selected just because it is installed. The two serializers disagree on some payloads: `NaN` and `Infinity` become `null`, and a non-string dict key raises instead of being coerced. Auto-selecting on importability would let an unrelated dependency change what logs say, or turn a working log call into an exception. The choice stays explicit, and the reasoning is now written down. ([#667](https://github.com/grelinfo/grelmicro/issues/667))
* 📝 Put the opt-in warning above every environment variable table, written once and included, so a reader who lands on a module page from a search engine sees it without following a link. The logging page also no longer claims every knob is an environment variable without saying when they are read. ([#662](https://github.com/grelinfo/grelmicro/issues/662))

## 0.35.0 - 2026-08-05

### Upgrading

**`/healthz` stopped sending null fields.** A check that passed no longer carries `error`, and a check with no details no longer carries `details`.

```diff
-{"status": "ok", "critical": true, "error": null}
+{"status": "ok", "critical": true}
```

A consumer that reads `error` unconditionally needs to treat it as absent:

```python
error = check.get("error")  # None when the check passed
if error is not None:
    alert(name, error)
```

A failing check still carries `error`, and `status` and `critical` are still on every entry, so a dashboard reading those needs no change.

**A `HealthChecks` no longer removes your backends.** Listing any Component used to switch provider auto-registration off entirely, so an app that added health checks lost its cache and locks with no warning. If you listed components explicitly only to work around that, the short form works now:

```diff
-micro = Grelmicro(uses=[
-    Coordination(redis), Cache(redis), RateLimiterRegistry(redis),
-    CircuitBreakerRegistry(redis), tasks, health,
-])
+micro = Grelmicro(uses=[redis, tasks, health])
```

Explicit components still win for their own kind, so mixed wiring keeps working untouched. Two or more providers still fill no defaults, so `HealthChecks(auto_health=True)` across several providers is unchanged.

**Redis credentials split across two variables now work.** `REDIS_PASSWORD` next to a `REDIS_URL` was read and then dropped, so the client connected unauthenticated. If you worked around that by building the provider from explicit settings, the plain form is enough again:

```diff
-provider = RedisProvider.sentinel(
-    sentinels=[("host", 26379)], service_name="mymaster", password=settings.password
-)
+provider = RedisProvider()
```

```bash
REDIS_URL=redis+sentinel://a:26379,b:26379/mymaster/0
REDIS_PASSWORD=...
```

That URL is the second half: `redis+sentinel://` and `redis+cluster://` are now accepted from the environment and from `RedisConfig`, including the multi-host form, so the topology no longer has to be hard-coded to be expressible.

One combination newly raises instead of passing silently: a URL that already carries credentials **and** a separate `REDIS_PASSWORD`. Keep the password in one place. The same applies to `VALKEY_*`.

### Breaking

* 💥 Back off per kind instead of disabling every provider default. Listing any Component used to turn provider auto-registration off entirely, so adding a `HealthChecks` silently removed the cache and the locks and the first `Lock("cart")` raised, naming the lock rather than the health registry that caused it. A Component now claims its own kind and the Provider fills the rest, which reads as one sentence: explicit wins, the Provider fills the rest. A Component of a kind no Provider serves (`HealthChecks`, `Log`, `Trace`) claims nothing and suppresses nothing. Two or more Providers still fill no defaults, since neither can be the default for a kind they both serve, so `HealthChecks(auto_health=True)` across several Providers is unchanged. ([#655](https://github.com/grelinfo/grelmicro/issues/655))
* 💥 Leave `error` and `details` out of a `/healthz` check that has neither. A passing check sent `"error": null` on every poll, and with details enabled it sent `"details": null` too, so the highest-frequency response in the service spent bytes reporting that nothing happened. A passing check is now `{"status": "ok", "critical": true}`, and a failing one still carries its `error`. The OpenAPI schema types both fields as a plain string and object instead of promising a nullable value that never arrives. Read an absent `error` as a pass. A consumer that required the key needs updating. ([#649](https://github.com/grelinfo/grelmicro/issues/649))

### Features

* ✨ Skip a `None` entry in `Grelmicro(uses=[...])` and `Bulkhead(uses=[...])`. A component registered only for one backend now stays a plain expression, `uses=[Log(), redis if backend == "redis" else None]`, instead of a star-unpacked conditional or a helper function. `micro.use(None)` still raises, because a single call can be guarded with `if`. ([#646](https://github.com/grelinfo/grelmicro/issues/646))
* ✨ Export `Usable`, the type of everything `uses=` accepts. Building the list in a variable did not type-check, because `Component` was the only exported name and a `Provider` is not a `Component`. Annotate it `list[Usable]` and append either. ([#646](https://github.com/grelinfo/grelmicro/issues/646))

### Fixed

* 🔒 Apply `REDIS_PASSWORD` to a `REDIS_URL` that carries no credentials. The password was read, validated and then dropped, so the client connected unauthenticated and the first command failed with `NOAUTH`, pointing at Redis rather than at the configuration. Host in a ConfigMap and password in a Secret is the shape the config docs recommend, and it was the one shape that did not work. A URL that already carries credentials plus a separate password now raises instead of silently preferring one, since the two can disagree. ([#653](https://github.com/grelinfo/grelmicro/issues/653))
* 🐛 Accept `redis+sentinel://` and `redis+cluster://` from `REDIS_URL`, `RedisConfig`, and `VALKEY_URL`. The environment and `from_config` paths validated against a stricter type than the constructor, so the topology that most needs environment configuration was the one that could not be expressed there. Multi-host authorities such as `redis+sentinel://a:26379,b:26379/mymaster/0` validate too. All three paths now share one URL type, so they cannot drift apart again. ([#654](https://github.com/grelinfo/grelmicro/issues/654))
* 🐛 Restore the component registry when an `override(...)` component fails to open. The registry was mutated one component at a time before the restore was armed, so a mock that raised on `__aenter__` stayed installed for the rest of the `async with micro:` block. The next lookup resolved the broken mock instead of the real component, with nothing reporting it, which in a session-scoped fixture leaked into every later test. ([#651](https://github.com/grelinfo/grelmicro/issues/651))
* 🐛 Instantiate a bare class passed to `Bulkhead(uses=[...])`. The parameter documented the same shape as `Grelmicro(uses=[...])`, which accepts a class with no parens, but the bulkhead entered the class object itself and failed on startup. ([#646](https://github.com/grelinfo/grelmicro/issues/646))
* 🐛 Reject `micro.use(None)` with a message naming the fix. It appended `None` to the item list and failed later inside the app lifecycle, pointing at nothing. ([#646](https://github.com/grelinfo/grelmicro/issues/646))

### Docs

* 📝 Show how to register a component conditionally, in [Wiring an App](wiring.md#register-something-conditionally). Covers the inline `None` entry, the `Usable`-annotated list, and how a Provider registers differently through `use` than inside `uses=`. ([#646](https://github.com/grelinfo/grelmicro/issues/646))
* 📝 Add a Providers recipe for taking the managed connection and nothing else. Application state that is not a cache, a lock, or a rate limiter is still yours to read and write through `provider.client`, with the lifecycle already handled. ([#646](https://github.com/grelinfo/grelmicro/issues/646))
* 📝 Document the `/healthz` report body, with a passing and a failing check side by side. ([#649](https://github.com/grelinfo/grelmicro/issues/649))
* 📝 Keep adapter classes out of the examples a first-time reader meets. The guide opened with `MemoryLeaderElectionAdapter()` and `Cache(MemoryCacheAdapter())` before the reader had any reason to know what an adapter is. Every quick start now names a provider once, `uses=[MemoryProvider()]` or `uses=[redis]`, and the pattern follows with no wiring in sight. Adapter references in the snippets went from 56 to 8, and the 8 that remain are Kubernetes and the outbox memory backend, where choosing a backend is the subject. Nothing about the API changed, so no code needs updating. ([#644](https://github.com/grelinfo/grelmicro/issues/644))

## 0.34.3 - 2026-08-05

### Security

* 🔒 Point client address identity checks at `forwarded` instead of `degraded`. `degraded` is False for `UNTRUSTED_PEER`, so one mistyped CIDR in `TrustedProxies` left every request carrying the proxy's own address, and the guard the docs recommended admitted it. The private network gate in the health docs then showed details to everyone, which is the bypass `degraded` was added to close. `forwarded` is True for `RESOLVED` alone, so it refuses that request. The docstrings and the reason table now say which outcomes mean the peer is the caller and which mean the address is one of your own proxies. If nothing fronts your app, `forwarded` is never True and there is nothing to gate on, so read [which check to use](clientip.md#which-check-to-use) before copying the new guard into a direct deployment. ([#636](https://github.com/grelinfo/grelmicro/issues/636))

### Features

* ✨ Log an untrusted peer that sends a non-empty `X-Forwarded-For` while `TrustedProxies` is not empty. That combination is either a caller sending the header directly or a proxy of yours missing from the trusted set, and the misconfiguration had no other symptom. The `grelmicro.clientip` logger gets one line per peer, for at most eight peers, so a busy proxy cannot flood it and a caller probing the header cannot take the line your own proxy needs. ([#636](https://github.com/grelinfo/grelmicro/issues/636))
* ✨ Cache an async generator with `@cached`. Iterating the decorated producer streams its items and stores the assembled list once it finishes, and `collect()` reads that same entry whole, so a streaming endpoint and a buffered one share one producer, one key and one execution. Only a completed sequence is stored, so a reader that stops early and a producer that raises part way both leave the key untouched rather than publishing a truncated result. ([#501](https://github.com/grelinfo/grelmicro/issues/501))

### Fixed

* 🐛 Report a truncated forwarded chain as `TOO_MANY_ENTRIES`. The reason existed but was never returned. A header longer than `max_entries` whose read window held only trusted proxies came back as `CHAIN_EXHAUSTED`, which claims every entry was seen. ([#636](https://github.com/grelinfo/grelmicro/issues/636))
* 🐛 Stop `@cached` hanging on a generator function. An async generator is not a coroutine function, so it took the sync wrapper, which blocks its own thread waiting on the cache loop. Decorating one wedged the event loop on the first call, with no error. Async generators are now supported, and a sync generator raises at decoration time, since it yields its items once and a cached one would replay as empty. ([#501](https://github.com/grelinfo/grelmicro/issues/501))

## 0.34.2 - 2026-08-02

### Security

* 🔒 Refuse an idempotency key that cannot separate one caller from another. A `key_maker` reading a value that was not set yet folded `None` into the key, so every caller shared one entry and could replay each other's stored response, while the request still answered `200`. A key that is partly missing does not fail, it merges, and the widening was invisible. `IdempotencyMiddleware` now raises `IdempotencyKeyMakerError` when the key is empty, drops the client's key, or carries an unresolved `None`.
* 🔒 Stop the multi-tenant `key_maker` example reading an unauthenticated header. It took the tenant from `X-Tenant`, which the client sets, so a caller could name the tenant whose entry they read. It now folds in an authenticated identity, uses a separator an identity cannot contain, and raises rather than building a partial key. The docs also say plainly that a client address is not a tenant identity, because carrier-grade NAT puts many subscribers behind one.

### Docs

* 📝 Say that a `key_maker` reading the scope needs its source middleware outside `IdempotencyMiddleware`. `ClientAddressMiddleware` added the wrong way round leaves `client_address` unset when the key is built, so the key folds in `None`, every caller shares one entry, and the request still answers `200`. A key that looks like it separates callers and does not is worse than no `key_maker` at all.
* 📝 Warn that a mounted sub-application does not fail loudly. A mount is an ordinary call in the same task, so the host's request scope is still bound inside it. A sub-application that forgot `install` therefore resolves against the host's components rather than raising, and two applications that look isolated share one store with nothing reporting it. `check_ambient_binding` catches it, per app.

### Internal

* 👷 Run the Python matrix in the release preflight. `just release-check` tested only the primary Python, so 0.34.0 passed every check it ran and still failed its release on 3.14, which burned the version. The new `just test-matrix` runs the unit and integration tiers on every other Python in the matrix, and reads that list out of the workflow so the preflight cannot drift away from what CI runs.

## 0.34.1 - 2026-08-02

### Internal

* ✅ Stop the release matrix failing on tests that were racing their own lease. Two runs of the 0.34.0 release failed on Python 3.14, each on a different test. A leader election test tripped a 5s per-test timeout, and a lock test asserting `extend()` keeps its fencing token got a fresh one instead, because the 10 ms lease had already lapsed and `extend` re-acquired. Neither was a product bug: 3.14 runs the modules about twice as slow as 3.12, on a runner already oversubscribed by `-n auto`. The affected tests now use the generous-lease fixture the file already had for this, and the timeout that only guards against hangs is generous enough to survive the matrix.

## 0.34.0 - 2026-08-02

Tagged but never published. The release run failed on the Python 3.14 matrix
before the publish step, so this version does not exist on PyPI. Everything
below ships in 0.34.1.

### Breaking

* 💥 Refuse `@cached` on a method unless it names its key. The default key is the `repr()` of every argument, and on a method that includes `self`, which was wrong in both directions: two instances whose `repr()` matched shared one entry, so a call on one returned the other's value, and an instance using the default `repr()` carried a memory address, so its key changed on every restart. Neither said anything. Decorating a method without `key=` or `key_maker=` now raises `TypeError` at decoration time. Pass a key naming what identifies the entry, such as `key="repo:{user_id}"`. ([#600](https://github.com/grelinfo/grelmicro/issues/600))

### Fixed

* 🐛 Keep the grelmicro request scope outside every other middleware. `IdempotencyMiddleware` had to be added before `micro.install(app)`, and the wrong order raised nothing at setup, so the first failure arrived in production from a client that actually sent `Idempotency-Key`. `install` now places the binding middleware outermost when the stack is built, so either order works, and reads the placement back on startup so a stack that still ends up wrong raises `AmbientBindingError` at boot. ([#599](https://github.com/grelinfo/grelmicro/issues/599))
* 🐛 Start more than one worker against a fresh Postgres. `CREATE TABLE IF NOT EXISTS` checks and creates in two steps, so two workers starting together both passed the check and one crashed on the row type the table creates. Every Postgres adapter now installs its schema under an advisory lock, as the outbox already did. ([#595](https://github.com/grelinfo/grelmicro/issues/595))
* 🐛 Give each worker of a pre-fork server its own coordination identity. `gunicorn --preload` builds the application once and forks, so every child inherited the identity generated in the parent. Two workers presented the same lock token, and every child read the leader record holder as itself, so all of them led at once. A child now appends its own random suffix. `uvicorn --workers N` spawns instead of forking and is unaffected. ([#595](https://github.com/grelinfo/grelmicro/issues/595))
* 🐛 Report a task fire that never ran. `grelmicro.task.runs` only counted fires that reached the body, so a schedule backend or a lock that stopped answering left no metric at all and looked exactly like a task with nothing due. A fire now always lands on the counter: `coordination_error` when coordination failed, `missed` when no worker ran it, and `skipped` when a peer handled it. The bare total counts more than it did, so read the [migration note](migration.md#0-34-task-run-outcomes) if a chart treats it as the run rate. ([#605](https://github.com/grelinfo/grelmicro/issues/605))
* 🐛 Warn when a cron fire is dropped for coming back too late. Past `misfire_grace_seconds` the fire was skipped with no log and no metric, so a task that never replayed said nothing at all. ([#605](https://github.com/grelinfo/grelmicro/issues/605))
* 🐛 Record a fire that never reached the body on `last_fire`. An introspection endpoint reading it during a coordination outage reported the previous successful fire and looked healthy. ([#605](https://github.com/grelinfo/grelmicro/issues/605))

### Internal

* 👷 Add `just` recipes for the release path. `just release-check <version>` runs what the Release workflow runs, before the tag exists, so a failure costs nothing rather than burning an immutable tag. `just verify-release <version>` downloads a published wheel and sdist and verifies build provenance for each, which turns the SLSA Build Level 2 badge into something anyone can check. `just release-notes <version>` prints the changelog section the GitHub Release body should hold. ([#584](https://github.com/grelinfo/grelmicro/issues/584))
* ✅ Pin both sides of the span exception check, so the coverage gate stops failing at random. Only one side had a test of its own. The other was covered whenever some unrelated test happened to raise inside a non-recording span, which depends on the order `pytest-randomly` picks, so a run could report `_span.py` at 96% and fail `--fail-under=100` on a pull request that changed nothing near it.
* 🧪 Verify the Patterns across real process boundaries. A new multiprocess tier races worker processes against Redis, so cross-process exclusion, single leadership, and the per-worker memory adapters are asserted rather than read off the code. The demo smoke stack now runs two uvicorn workers and checks the rate limit holds across both. ([#595](https://github.com/grelinfo/grelmicro/issues/595))

### Docs

* 📝 Name the hazard `env_load=False` guards against. Env reads fill every field the caller did not pass, so a config half taken from a `Settings` object silently gets the rest from the environment. A `Settings` default that differs from the environment is dropped without a word. ([#606](https://github.com/grelinfo/grelmicro/issues/606))
* 📝 Treat outbox retention as a decision the caller makes. A payload sits in the database until its row is deleted, so a single-use secret is at rest for as long as the row lives. The default deletes a delivered row, which is the safe end, but a default is not a guarantee. Pin `keep_delivered` rather than inherit it, and note that dead rows are never purged automatically. ([#607](https://github.com/grelinfo/grelmicro/issues/607))
* 📝 Say that a stored idempotent response sits at rest for `ttl`. The same audit: the middleware stores the whole response, so `ttl` is a retention window and not only a replay window. ([#607](https://github.com/grelinfo/grelmicro/issues/607))
* 📝 Correct the pre-fork guidance for coordination. It told the reader to pass an explicit `worker` identity, which every child inherits just the same, so the advice turned a likely collision into a certain one. ([#595](https://github.com/grelinfo/grelmicro/issues/595))
* 📝 Say that `Bulkhead.max_concurrent` and `Shield.max_rate` are per worker process. Both read as a deployment-wide ceiling, so four workers quietly gave the dependency four times the configured number. ([#595](https://github.com/grelinfo/grelmicro/issues/595))

## 0.33.0 - 2026-07-31

### Features

* ✨ Add `ClientAddress.degraded`, which marks a result whose address is the connecting peer rather than the caller. Anything treating the address as an identity must refuse when it is set. ([#609](https://github.com/grelinfo/grelmicro/issues/609))
* ✨ Add `grelmicro.clientip`, which resolves the real client address behind a reverse proxy. `X-Forwarded-For` is append-only, so its leftmost entry is attacker-controlled. The resolver reads the header only when the connecting peer is a trusted proxy, walks right to left, and returns the first entry no trusted proxy wrote. The trusted set is required and there is no wildcard. ([#609](https://github.com/grelinfo/grelmicro/issues/609))

### Security

* 🔒 Refuse to show health details when the client address is a fallback. Resolving alone was not enough: a forged chain that could not be believed still returned the proxy's own private address, so the `is_private` check admitted everyone again. `ClientAddress.degraded` marks every such case. ([#609](https://github.com/grelinfo/grelmicro/issues/609))
* 🔒 Stop the health-detail example from showing details to everyone behind a proxy. It gated on `request.client.host` being private, which is the proxy's own address for every external caller, so `is_private` was true for all of them. It now resolves the client. The rate limiter example keyed on the same value, giving every caller one shared bucket. ([#609](https://github.com/grelinfo/grelmicro/issues/609))

### Internal

* 👷 Enforce the coverage total on the pull request that changes code, not in the next nightly. The slow and integration tiers now run on a code-touching pull request or push, on the primary Python only, so the combined 100% total is measurable there. The Python matrix and the demo tier stay on the nightly, dispatch, and release paths. About two minutes per pull request. ([#602](https://github.com/grelinfo/grelmicro/issues/602))
* 👷 Stop a Codecov upload from failing CI. The test-results action crashed on a transient network error, which fails the step whatever `fail_ci_if_error` says, so a green test run with a passing coverage gate was reported as a failure. Uploads are telemetry and now cannot gate a build. ([#602](https://github.com/grelinfo/grelmicro/issues/602))

### Docs

* 📝 Write down the pre-1.0 deprecation policy. A rename before 1.0 is a clean cut, with the reasoning recorded so the question does not get re-litigated per rename. ([#613](https://github.com/grelinfo/grelmicro/issues/613))
* 📝 Add a [migration page](migration.md), one note per minor from 0.30 onward, listing only what an upgrade requires. It leads with a symptom table, so an adopter several versions behind can match the error they see instead of reconstructing the path from every release's notes. ([#608](https://github.com/grelinfo/grelmicro/issues/608))

## 0.32.9 - 2026-07-30

### Fixed

* 🐛 Report a failed background cache refresh. The task's exception was discarded, so a permanently failing recompute silently degraded every hot key back to a cold miss. It now logs a warning naming the key and records `grelmicro.cache.early_refreshes` with `outcome` and `error.type`. ([#605](https://github.com/grelinfo/grelmicro/issues/605))
* 🐛 Hold a strong reference to a background cache refresh task. The event loop keeps only weak references, so the task could be collected before finishing and would then report nothing at all. ([#605](https://github.com/grelinfo/grelmicro/issues/605))
* 🐛 Report a failed `Shield` cache write. It was logged at debug with no counter, so the copy the shield serves when the primary fails could stop being written and nothing said so until the incident it exists for. ([#605](https://github.com/grelinfo/grelmicro/issues/605))
* 🐛 Rate-limit the `Shield` cache warning to once a minute per shield, and report a failing cache read as well as a write. A cache write rides along with every successful call, so an unreachable store would otherwise log once per request. The counter still records every failure. ([#605](https://github.com/grelinfo/grelmicro/issues/605))
* 🐛 Remove the Postgres outbox termination listener before releasing the connection. asyncpg calls it on any close, so a clean shutdown warned about a lost listener, and a shared pool could warn again when it later recycled that connection. ([#605](https://github.com/grelinfo/grelmicro/issues/605))
* 🐛 Back off when an outbox backend's `wait_notify` fails instantly. The relay returned with no delay and spun its claim query, one warning and one query per iteration. ([#605](https://github.com/grelinfo/grelmicro/issues/605))
* 🐛 Report a lost Postgres outbox listener connection. Delivery silently fell back to polling, bounded by `poll_interval`, until the process restarted. ([#605](https://github.com/grelinfo/grelmicro/issues/605))
* 🐛 Report a crashed outbox relay or purge loop when it crashes. `asyncio.wait` never re-raises, so a crash stopped delivery for the life of the process, and reporting it only at shutdown would have arrived long after it mattered. Relay wait errors moved from debug to warning. ([#605](https://github.com/grelinfo/grelmicro/issues/605))

### Docs

* 📝 State the background-failure contract: work with no caller to raise into always becomes observable through a counter, a warning, or a health degradation, never a silent suppression. ([#605](https://github.com/grelinfo/grelmicro/issues/605))

## 0.32.8 - 2026-07-30

### Internal

* 👷 Enforce the 100% coverage claim in CI. Every tier reset coverage instead of accumulating, so CI only ever measured the unit tier and no `fail-under` gate ran there at all. The claim was checked only by a local hook that pre-commit.ci skips. The slow and integration tiers now append, and the full path fails under 100%. ([#602](https://github.com/grelinfo/grelmicro/issues/602))
* 👷 Make Codecov's project status advisory. It compared a pull request's unit-only coverage against a base built from the full tier, so it reported a regression that was a tier mismatch rather than a real one. ([#602](https://github.com/grelinfo/grelmicro/issues/602))

## 0.32.7 - 2026-07-30

### Features

* ✨ Add `refresh()` to a `@cached` function. It recomputes for the given arguments, overwrites the stored entry, and returns the new value, so an endpoint honouring `Cache-Control: no-cache` keeps the decorator's key handling and tag invalidation. Concurrent refreshes each recompute rather than folding, and an error propagates instead of serving stale. ([#500](https://github.com/grelinfo/grelmicro/issues/500))
* ✨ Add `IdempotencyMiddleware`. A request carrying an `Idempotency-Key` header runs once, and a retry replays the stored response without reaching the handler. Pure ASGI, so it works on FastAPI, Starlette, and Litestar alike. ([#503](https://github.com/grelinfo/grelmicro/issues/503))
* ✨ Bound the single-flight wait with `wait_timeout=` on an `Idempotency` block and on `run()`. A duplicate that waits longer than that raises `IdempotencyWaitTimeoutError`, which subclasses `TimeoutError`, instead of holding the caller indefinitely. ([#503](https://github.com/grelinfo/grelmicro/issues/503))
* ✨ Add `document_idempotency(app)`, which describes the installed `IdempotencyMiddleware` in the OpenAPI schema. A middleware is invisible to the generated schema, so a client built from it never learns the header exists. ([#503](https://github.com/grelinfo/grelmicro/issues/503))

### Fixed

* 🐛 Accept a SQLAlchemy-style Postgres URL. `PostgresProvider` took `postgresql+asyncpg://` without complaint and then failed at connect time, so every adopter stripped the driver suffix by hand. The suffix is now dropped when the URL is resolved, from a keyword, the environment, or a config, whatever the scheme's case. ([#596](https://github.com/grelinfo/grelmicro/issues/596))
* 🐛 Type the helpers a `@cached` function exposes. `cached()` returned a plain `Callable`, so `cache_info()` and `cache_clear()` were documented but invisible to type checkers, and calling them failed a downstream `ty` or `mypy` run. It now returns `CachedFunction`. ([#500](https://github.com/grelinfo/grelmicro/issues/500))
* 🐛 Release the single-flight lock correctly when an `Idempotency` block is cancelled while acquiring it. The lock was bound to the block before it was held, so the cleanup path tried to release a lock the block did not own and raised `LockNotOwnedError` over the original error. ([#503](https://github.com/grelinfo/grelmicro/issues/503))
* 🐛 Release the in-process idempotency lock even when the distributed release is cancelled mid-flight. A cancellation there left the key locked for the life of the process. ([#503](https://github.com/grelinfo/grelmicro/issues/503))

### Internal

* 🚨 Satisfy the stricter type narrowing in `ty` 0.0.62. Two existing call sites lost a type parameter through `isinstance`, which failed the lint gate on the dependency bump rather than in any user-visible way. ([#598](https://github.com/grelinfo/grelmicro/pull/598))

### Docs

* 📝 Document how to report a bug: where to file, what a report needs, and what happens after you file it. ([#592](https://github.com/grelinfo/grelmicro/pull/592))
* 📝 Add GitHub issue forms for bug reports and feature requests, so a report arrives with the version, the backend, and a runnable reproduction. ([#592](https://github.com/grelinfo/grelmicro/pull/592))
* 📝 Add the OpenSSF Best Practices badge, now passing at 100%. ([#592](https://github.com/grelinfo/grelmicro/pull/592))

## 0.32.6 - 2026-07-29

### Fixed

* 🐛 Take the state lifetime off the in-memory circuit breaker's admission path. Every call recomputed the lifetime and read the clock, which made `try_acquire` 2.4x slower from 0.32.2 on. Entries now carry an absolute deadline, and a closed circuit admits from one dictionary lookup. ([#582](https://github.com/grelinfo/grelmicro/issues/582))

### Docs

* 📝 Lead the README with one sentence that says what grelmicro is, and add the SLSA Build Level 2 badge. ([#581](https://github.com/grelinfo/grelmicro/pull/581))

## 0.32.5 - 2026-07-28

### Security

* 🔒 Attest build provenance for every release. Each published artifact now carries a provenance attestation signed on GitHub infrastructure separate from the build job, and the release verifies it before publishing, so a missing or invalid attestation fails the release. ([#575](https://github.com/grelinfo/grelmicro/pull/575))

### Docs

* 📝 Move the documentation site to [grelmicro.grel.info](https://grelmicro.grel.info). The old GitHub Pages address redirects, so existing links keep working. ([#577](https://github.com/grelinfo/grelmicro/pull/577))
* 📝 Add a root `CHANGELOG.md` pointing to the published changelog, so tools that look for one at the repository root find it. ([#576](https://github.com/grelinfo/grelmicro/pull/576))

## 0.32.4 - 2026-07-28

### Docs

* 📝 Document the recommended `CircuitBreaker` lifecycle. Build one per name at module level. The circuit lives in the backend keyed by name, but `last_error`, the call totals, and the cached state are per instance, so a per-request breaker reports empty metrics and logs a transition that never happened. ([#497](https://github.com/grelinfo/grelmicro/issues/497))
* 📝 Correct the cross-replica story for cron tasks. The task page said at-most-once always needs a `TaskLock` or `LeaderElection`, which is true for `every` and false for `cron`. Cron claims each fire against the schedule backend, so a wired `Coordination` component is all it needs. ([#502](https://github.com/grelinfo/grelmicro/issues/502))
* 📝 Warn against gating a cron body on leadership. Winning the claim advances the durable state before the body runs, so an early return consumes the fire without doing the work. ([#502](https://github.com/grelinfo/grelmicro/issues/502))
* 📝 Document what the `auto` trace exporter selects. It resolves to OTLP HTTP or to the no-op and never to gRPC, whatever the endpoint URL or the installed exporter packages. ([#498](https://github.com/grelinfo/grelmicro/issues/498))

## 0.32.3 - 2026-07-28

### Fixed

* 🐛 Bound the cache cleanup sweep. It deleted every expired row in one statement, so a large backlog held one long delete against the table. Each pass now takes at most 1000 rows and the interval is jittered, so replicas do not sweep in lockstep. ([#496](https://github.com/grelinfo/grelmicro/issues/496))
* 🐛 Log cache cleanup failures instead of swallowing them. A failing sweep was silently suppressed, so a cache that stopped reclaiming disk gave no signal. ([#496](https://github.com/grelinfo/grelmicro/issues/496))

## 0.32.2 - 2026-07-28

### Features

* ✨ Add `CircuitBreaker.keyed(key)` to give each tenant, endpoint, or model its own circuit, with independent counters, state, and cool-down. ([#496](https://github.com/grelinfo/grelmicro/issues/496))
* ✨ Circuit breakers now reclaim their stored state instead of keeping it forever, so a dynamic key set no longer grows the backend without bound. Redis expires the key, Postgres and SQLite sweep hourly via `cleanup_interval=`. ([#496](https://github.com/grelinfo/grelmicro/issues/496))

### Upgrading

Circuit-breaker rows written before this release carry no activity timestamp, so an already-open circuit on Postgres or SQLite reads as expired the first time the new code touches it and starts again from `CLOSED`. This happens once, on the first call after the upgrade. Circuits held open by `isolate()` are unaffected.

## 0.32.1 - 2026-07-28

Re-cut of 0.32.0, which never reached PyPI. A flaky test failed the release run, so publishing was skipped. The 0.32.0 tag is immutable, so the same contents ship here. See [0.32.0](#0320-2026-07-28) for the changes.

### Internal

* ✅ Stop `test_lock_acquire_nowait_would_block` racing its own lease. The test asserts that a second worker is blocked, but held the lock on a 10 ms lease, so a loaded runner could let the lease lapse before the second worker tried. It then acquired cleanly and `WouldBlock` never fired, which failed the 0.32.0 release on Python 3.14. Contention tests now use a lease that outlives scheduling jitter, matching the from-thread twin.

## 0.32.0 - 2026-07-28

### Breaking

* 💥 Credential-carrying URL fields are now `SecretUrl`: `url` on `PostgresConfig` and `RedisConfig`, and `endpoint` on `TraceConfig` and `MetricsConfig`. Each `headers` value on `TraceConfig` and `MetricsConfig` is now a `SecretStr`. Passing a plain string still works, but reading the value back needs `.get_secret_value()`. ([#550](https://github.com/grelinfo/grelmicro/issues/550))
* 💥 `SQLiteLockAdapter` and `SQLiteScheduleAdapter` now take `provider=` instead of `path=`, like every other SQLite adapter. Replace `SQLiteLockAdapter("app.db")` with `SQLiteLockAdapter(provider=SQLiteProvider("app.db"))`, or pass the provider to `Coordination(sqlite)` and let it build both. A missing path now raises `SettingsValidationError` instead of `CoordinationSettingsValidationError`. ([#546](https://github.com/grelinfo/grelmicro/issues/546))

### Features

* ✨ Add `grelmicro.types.SecretUrl`, a URL that never shows its credentials. It displays the URL with the userinfo password and credential-like query values replaced by `***`, so the scheme, host, and path stay readable in logs. Parametrize it with any pydantic URL type to keep that type's validation: `SecretUrl[RedisDsn]`, `SecretUrl[PostgresDsn]`, or a bare `SecretUrl` for any URL. ([#550](https://github.com/grelinfo/grelmicro/issues/550))

### Fixed

* 🐛 Capture the running event loop in `PostgresLockAdapter` and `PostgresScheduleAdapter`. Both protocols require a `_loop` attribute, but neither adapter set it, so `Lock.from_thread` and `TaskLock.from_thread` raised `AttributeError` against a Postgres backend instead of working. Every other backend already captured it. ([#541](https://github.com/grelinfo/grelmicro/issues/541))
* 🐛 Share one SQLite connection across every component on the same file. `SQLiteLockAdapter` and `SQLiteScheduleAdapter` opened their own connection, so an app pairing a lock with a cache held two. They now borrow the provider's connection and shared lock, so `Grelmicro` can dedupe them onto one provider like every other adapter. ([#546](https://github.com/grelinfo/grelmicro/issues/546))
* 🐛 Adopt the provider behind a `Coordination` schedule backend. Provider discovery walked the lock and election backends only, so a schedule-only `Coordination` left its provider unopened and duplicate providers undeduped. ([#546](https://github.com/grelinfo/grelmicro/issues/546))

### Security

* 🔒 Mask credentials embedded in connection URLs. `url` on `PostgresConfig` and `RedisConfig`, and `endpoint` and `headers` on `TraceConfig` and `MetricsConfig`, appeared in full in `repr()`, `model_dump()`, and `model_dump_json()`. Nothing changes on the wire. ([#550](https://github.com/grelinfo/grelmicro/issues/550))
* 🔒 Stop echoing rejected values from `PostgresConfig`, `RedisConfig`, `TraceConfig`, and `MetricsConfig`. A mistyped URL carried its password into the `ValidationError` text. ([#550](https://github.com/grelinfo/grelmicro/issues/550))

### Internal

* ✅ Add `tests/test_adapter_contracts.py`, which asserts every first-party adapter initializes `_loop` and captures the running loop on `__aenter__`. A `Protocol` attribute annotation declares the requirement but never creates it, so both type checkers pass an adapter that omits it. ([#541](https://github.com/grelinfo/grelmicro/issues/541))
* 👷 Clear 16 modules from the mypy override ladder, leaving 3. The `uses=` resolver, the optional-import rebinding, and the `functools.wraps` returns now type-check under both checkers. ([#541](https://github.com/grelinfo/grelmicro/issues/541))
* 🔥 Drop a stale `# type: ignore` in `grelmicro/metrics/_component.py`. grelmicro suppresses with `# ty: ignore` only. ([#541](https://github.com/grelinfo/grelmicro/issues/541))
* ⬆️ Bump `ruff` to 0.16. Markdown formatting is now stable, so the formatter skips `*.md` and leaves the README and docs examples written as they read best. The new `CPY001` rule stays off: the MIT licence lives in `LICENSE`, not in a per-file header. ([#557](https://github.com/grelinfo/grelmicro/pull/557))
* 👷 Pin the `ty-check` pre-commit hook to `TY_MAX_PARALLELISM=1`, matching CI. ty resolves inference cycles in whichever order threads reach them, so a local run could flag a diagnostic that CI did not. ([#551](https://github.com/grelinfo/grelmicro/issues/551))
* 📝 List each Provider once in the SQLite and bulkhead examples. A Component that borrows a Provider already adopts its lifecycle, so the bare Provider beside it was redundant. The Redis and Postgres examples were already written this way. ([#559](https://github.com/grelinfo/grelmicro/pull/559))

## 0.31.0 - 2026-07-27

### Breaking

* 💥 Default `Metrics()` to the `auto` exporter. It exports over OTLP HTTP when an endpoint is configured and otherwise auto-disables into a true no-op, so an unconfigured `Metrics()` no longer falls back to `localhost:4318`. Register it unconditionally: an auto-disabled `Metrics` installs no provider and never conflicts with a second app. ([#508](https://github.com/grelinfo/grelmicro/issues/508))
* 💥 Credential fields are now `SecretStr`: `basic_auth_password` on `TraceConfig` and `MetricsConfig`, and `password` on `PostgresConfig` and `RedisConfig`. Passing a plain string still works, but reading the value back needs `.get_secret_value()`. ([#549](https://github.com/grelinfo/grelmicro/pull/549))

### Features

* ✨ Add `basic_auth=(username, password)` to `Metrics`, matching `Trace`. grelmicro builds the `Authorization: Basic` header and attaches it to the OTLP exporter directly, bypassing the fragile `OTEL_EXPORTER_OTLP_HEADERS` encoding. From the environment, set `GREL_METRICS_BASIC_AUTH_USERNAME` and `GREL_METRICS_BASIC_AUTH_PASSWORD`. ([#507](https://github.com/grelinfo/grelmicro/issues/507))

### Fixed

* 🏷️ Preserve the wrapped function's signature through the resilience decorators. Applying `Retry`, `Shield`, `Bulkhead`, `Timeout`, `CircuitBreaker`, or `Fallback` no longer erases the parameter and return types, so calls to a decorated function stay type-checked. ([#545](https://github.com/grelinfo/grelmicro/pull/545))
* 🐛 Declare `_loop` on `LockBackend`, `ScheduleBackend`, `CacheBackend`, and `CircuitBreakerBackend`. The attribute was already required in prose, so a third-party adapter that omitted it type-checked and then raised `AttributeError` on the first `from_thread` call. ([#540](https://github.com/grelinfo/grelmicro/pull/540))
* 🐛 Raise a clear error when `Lock.from_thread` or `TaskLock.from_thread` runs before the backend is opened, matching the cache and circuit-breaker behavior. ([#540](https://github.com/grelinfo/grelmicro/pull/540))
* 🐛 Point the circuit-breaker worker-thread error at `async with micro:` instead of `grelmicro.lifespan()`, which is not a public API. ([#540](https://github.com/grelinfo/grelmicro/pull/540))
* 🐛 Raise a clear error when installing the FastStream ambient middleware on an app with no broker, instead of an `AttributeError`. ([#540](https://github.com/grelinfo/grelmicro/pull/540))

### Security

* 🔒 Mask credentials in config objects. `basic_auth_password` on `TraceConfig` and `MetricsConfig`, and `password` on `PostgresConfig` and `RedisConfig`, were plain `str` and so appeared in `repr()`, `model_dump()`, and `model_dump_json()`. All four are now `SecretStr`. Nothing changes on the wire. ([#549](https://github.com/grelinfo/grelmicro/pull/549))

### Internal

* 🐛 Set `strict` directly on `LogTimeZoneType` instead of using `@timezone_name_settings`. The decorator's return type references the class it decorates, so `ty` resolved the subclass differently between parallel runs and failed about 9 runs in 10. Runtime behavior is unchanged. ([#549](https://github.com/grelinfo/grelmicro/pull/549))
* 📝 Document the `_loop` capture contract in the third-party adapter guide. The protocols require it, but the guide did not mention it, so a new adapter could follow the docs and still fail on the first `from_thread` call. ([#549](https://github.com/grelinfo/grelmicro/pull/549))
* ✅ Widen the circuit-breaker cool-down margins in the SQLite, Postgres, and Redis backend tests. The rejection assert now uses a 60s cool-down so a stalled runner cannot let the window elapse between two adjacent calls, and the elapse asserts wait 5x the cool-down instead of racing a 0.05s gap. ([#548](https://github.com/grelinfo/grelmicro/pull/548))
* 👷 Enable the Pydantic mypy plugin and shrink the mypy override ladder from 29 modules to 19. Ten modules now type-check under both checkers. ([#547](https://github.com/grelinfo/grelmicro/pull/547))
* ♻️ Declare `arbitrary_types_allowed` in the `_BaseShieldConfig` class kwargs instead of a second `model_config` assignment. Pydantic merged both, so the settings are unchanged. ([#547](https://github.com/grelinfo/grelmicro/pull/547))
* 🔥 Drop the inert mypy `# type: ignore` comments. grelmicro type-checks with ty, which never read them, and `ty check` stays clean without them. ([#539](https://github.com/grelinfo/grelmicro/pull/539))
* ✅ Add `tests/typechecking/`, a suite of `assert_type` claims on the public API, checked by both ty and mypy. grelmicro ships `py.typed`, so these annotations are part of the contract. ([#544](https://github.com/grelinfo/grelmicro/pull/544))
* 👷 Run mypy in CI alongside ty. The 30 modules that do not pass yet are listed in `[[tool.mypy.overrides]]` and tracked in [#541](https://github.com/grelinfo/grelmicro/issues/541). ([#544](https://github.com/grelinfo/grelmicro/pull/544))
* 🔥 Delete `grelmicro/metrics/_otel.py`, which nothing imported. ([#544](https://github.com/grelinfo/grelmicro/pull/544))
* ♻️ Return `OTel | None` from the private trace resolver so the handles narrow together, rather than a tuple of independently optional fields. ([#540](https://github.com/grelinfo/grelmicro/pull/540))

## 0.30.1 - 2026-07-18

### Fixed

* 🐛 Use `inspect.iscoroutinefunction` in `@cached`, so grelmicro runs clean on Python 3.14, where `asyncio.iscoroutinefunction` is deprecated. ([#532](https://github.com/grelinfo/grelmicro/pull/532))

## 0.30.0 - 2026-07-18

### Breaking

* 💥 Replace the idempotency `Operation.response` attribute with an `Operation.result()` method typed as the stored type, so the replay branch returns it without a cast. It is valid only on a replay: calling it on a first execution raises the new `IdempotencyStateError`. ([#504](https://github.com/grelinfo/grelmicro/issues/504))

### Fixed

* 🏷️ Preserve the decorated function's type through `@health.check`, so awaiting an async check directly type-checks without `# type: ignore`. ([#499](https://github.com/grelinfo/grelmicro/issues/499))

### Internal

* ♻️ Use the standard library `uuid.uuid7()` on Python 3.14+, so outbox ids stay monotonic within a millisecond. The vendored generator stays as the fallback for 3.12 and 3.13. ([#522](https://github.com/grelinfo/grelmicro/issues/522))
* ✅ Treat warnings as errors in the test suite and close the FastAPI health test client cleanly. ([#526](https://github.com/grelinfo/grelmicro/pull/526))
* ⬆️ Adopt `httpx2` in the test suite so Starlette's `TestClient` stops warning about httpx v1. ([#527](https://github.com/grelinfo/grelmicro/pull/527))

## 0.29.5 - 2026-07-18

### Security

* 🔒 Add a security policy with private vulnerability reporting. ([#523](https://github.com/grelinfo/grelmicro/pull/523))

### Docs

* 📝 Add a Contributing section to the README linking issues and the contributing guide. ([#523](https://github.com/grelinfo/grelmicro/pull/523))
* 📝 Add an `llms.txt` documentation index for LLM-friendly discovery. ([#524](https://github.com/grelinfo/grelmicro/pull/524))

### Internal

* 👷 Pin the demo Docker image by digest and track it with Dependabot. ([#523](https://github.com/grelinfo/grelmicro/pull/523))

## 0.29.4 - 2026-07-18

### Internal

* ⬆️ Bump the Python dependency group (including `redis` 8.0.1, `pytest`, `ruff`, and `ty` 0.0.58), the GitHub Actions, and the pre-commit hooks. ([#514](https://github.com/grelinfo/grelmicro/pull/514), [#510](https://github.com/grelinfo/grelmicro/pull/510), [#488](https://github.com/grelinfo/grelmicro/pull/488))
* 🚨 Adapt to `ty` 0.0.58: type the `Component` protocol's `name` as a read-only property and trim stale `ty: ignore` directives.

### Docs

* 📝 List the transactional outbox among the modules in the README intro.

## 0.29.3 - 2026-07-18

### Features

* ✨ Read the Postgres database name from `POSTGRES_DATABASE` too, not only `POSTGRES_DB`, so the longer spelling works from the environment. `DB` still wins when both are set. ([#518](https://github.com/grelinfo/grelmicro/issues/518))

## 0.29.2 - 2026-07-18

### Features

* ✨ Let the outbox auto-purge delivered rows. `keep_delivered` now accepts a `timedelta`: the relay keeps delivered rows for that window and purges them in the background, so retention needs no scheduled job. `True` still keeps them for good and `False` still deletes on delivery.
* ✨ Add `Outbox.current()` to resolve the app-registered outbox, so a producer can `publish` without holding the instance or a config-bound singleton. ([#517](https://github.com/grelinfo/grelmicro/issues/517))
* ✨ Add `PostgresOutboxAdapter.create_table_sql()` and `drop_table_sql()`, the exact DDL `auto_migrate` runs, so Alembic and other migration tools own the outbox schema with `auto_migrate=False`.

### Docs

* 📝 List the outbox in the README module table and align the outbox guide with the other module docs.

## 0.29.1 - 2026-07-18

### Features

* ✨ Add `command_timeout` to `PostgresProvider` (kwarg or `POSTGRES_COMMAND_TIMEOUT`) so a query against a frozen or unreachable Postgres raises `TimeoutError` in bounded time instead of hanging until the OS TCP timeout.
* ✨ Add the `outbox` module: a PostgreSQL transactional outbox that runs any async handler exactly after your transaction commits, at least once. `publish` stages a message in your own asyncpg, SQLAlchemy, or SQLModel transaction, and a relay delivers it with `FOR UPDATE SKIP LOCKED`, `NOTIFY` wakeups, a visibility lease, retries with backoff, and dead-lettering. Trace context and delivery metrics ride the [trace](tracing.md) and [metrics](metrics.md) components, and `purge` trims delivered and dead rows. Backend-first, so SQLite and MySQL can follow.

## 0.28.2 - 2026-07-05

### Fixed

* 🐛 Make idempotency storage atomic. The fingerprint guard is now written before the response and outlives it, so a failed guard write can never leave a response that a different payload could replay. ([#494](https://github.com/grelinfo/grelmicro/issues/494))
* 🐛 Guard the process-global active-app registry with a lock so two apps starting concurrently cannot both enter and clobber each other's `Log`/`Trace`/`Metrics` state. The second raises `MultipleActiveAppsError`. ([#494](https://github.com/grelinfo/grelmicro/issues/494))
* 🐛 Roll back an opened `Grelmicro` app when a later FastStream startup hook fails, so a failed startup no longer leaves the app open and registered. ([#494](https://github.com/grelinfo/grelmicro/issues/494))
* 🐛 Export `SQLiteCircuitBreakerAdapter` from `grelmicro.resilience`, matching the other circuit breaker adapters. ([#494](https://github.com/grelinfo/grelmicro/issues/494))

### Security

* 🔒 Make the `TaskLock` token nonce unpredictable (a process-local counter joined with random bytes) so an untrusted in-process caller cannot forge another handle's ownership token. ([#494](https://github.com/grelinfo/grelmicro/issues/494))

### Docs

* 📝 Point every ambient-miss `OutOfContextError` at `micro.install(app)`, and switch the README and first-steps examples to the one-call `micro.install(app)` form. ([#494](https://github.com/grelinfo/grelmicro/issues/494))
* 📝 Add a first-use mental model, an operator defaults reference, an API conventions page, an adapter import policy, and decision tables for rate limiter methods and task entry points. ([#494](https://github.com/grelinfo/grelmicro/issues/494))
* 📝 Warn that `/healthz` always returns each check's `error` string, and that cache keys and tags derived from untrusted input must stay bounded. ([#494](https://github.com/grelinfo/grelmicro/issues/494))

## 0.28.1 - 2026-07-05

### Features

* ✨ Make `Trace` a true no-op when the exporter auto-disables. With the default `auto` exporter and no endpoint, `Trace` installs no tracer provider, leaves the global untouched, runs no auto-instrumentation, and no longer counts against the single-active-app guard, so `Trace()` is safe to register unconditionally in dev, test, and CI. An explicit `exporter=none` still installs the provider. ([#487](https://github.com/grelinfo/grelmicro/issues/487))

### Fixed

* 🐛 Block a second app that installs `Metrics` while one is active, matching `Log` and `Trace`. `Metrics` owns the process-global meter provider, so two overlapping apps would clobber it.

### Docs

* 📝 Refresh stale docs: the optional-extras table, the capability matrix, the module lists, the `@cached` `lock` default, and the `DuplicateFilter` `ttl` field.

## 0.28.0 - 2026-06-28

### Breaking

* 💥 Rename the recurring-task decorator from `@task.interval(seconds=...)` to `@task.every(seconds=...)`, pairing it with `@task.cron(...)` as a verb-form family. The `seconds=` keyword is unchanged. Update your call sites.
* 💥 Move the framework integration modules into `grelmicro.integrations`. `grelmicro.fastapi` becomes `grelmicro.integrations.fastapi`, so `GrelmicroMiddleware` is now `from grelmicro.integrations.fastapi import GrelmicroMiddleware`, and the new FastStream wiring lives at `grelmicro.integrations.faststream`.
* 💥 Move the FastAPI health router from `grelmicro.health.fastapi` to `grelmicro.integrations.fastapi`, so all FastAPI integration code (middleware, install, health router) lives under `grelmicro.integrations`. Update `from grelmicro.health.fastapi import health_router` to `from grelmicro.integrations.fastapi import health_router`.
* 💥 Collapse the four `@task.every` lock passthroughs into one typed `lock=TaskLock(...)`. The `lease_duration`, `min_hold_duration`, `backend`, and `worker` parameters are removed. Pass a `TaskLock` instead, like `@task.every(seconds=60, lock=TaskLock(lease_duration=300))`. The lock keeps its default `"default"` name and is re-stamped to the task name, so you never repeat it. The lock's settings are authoritative. `leader=` and `sync=` stay separate, self-documenting parameters.
* 💥 `HealthChecks` now namespaces its env vars per instance: the default instance keeps `GREL_HEALTH_*`, but a named instance reads `GREL_HEALTH_{NAME_UPPER}_*` (matching `Lock`, `CircuitBreaker`, and the other named components). Update env vars for any named `HealthChecks`.
* 💥 The log filters now namespace env vars per instance. `GREL_DUPLICATE_FILTER_*` becomes `GREL_DUPLICATEFILTER_*` and `GREL_RATE_LIMIT_FILTER_*` becomes `GREL_RATELIMITFILTER_*`, with a new `env_name=` kwarg for a named instance (`GREL_DUPLICATEFILTER_{NAME}_*`). `DuplicateFilter.key_mode` also gains `logger`, `level`, and `global` so both filters share one vocabulary. Update env vars and `env_prefix=` overrides.
* 💥 Replace the six manual circuit-breaker control methods with two operator verbs. `isolate()` forces the breaker open until reset, `reset()` returns it to normal automatic operation starting `CLOSED`. Replace `transition_to_forced_open()` with `isolate()` and `restart()` with `reset()`. The remaining `transition_to_closed`, `transition_to_open`, `transition_to_half_open`, and `transition_to_forced_closed` are removed.
* 💥 `CircuitBreakerMetrics` and `ErrorDetails` are now frozen slotted dataclasses instead of Pydantic models. Attribute access is unchanged, but they no longer offer `.model_dump()` or Pydantic validation. This matches `CircuitBreakerSnapshot` and `CacheInfo` (read-models are dataclasses, Pydantic is reserved for serialization boundaries).
* 💥 `@cached` now folds concurrent misses of the same key in-process by default (`lock="local"` instead of `lock=False`). This removes a silent thundering-herd footgun at no I/O cost. Pass `lock=False` to restore the old behavior where every concurrent miss recomputes.
* 💥 Move the backend protocols out of the public `abc` submodules into private `_protocol` modules (`clock`, `config`, `coordination`, `task`), matching `cache` and `resilience`. `VirtualClock` likewise moves to `clock/_virtual.py`. The protocols stay exported from each package, so import them from the package (`from grelmicro.coordination import LockBackend`) instead of `grelmicro.coordination.abc`.
* 💥 `reconfigure()` now raises `CoordinationSettingsValidationError` (a `SettingsValidationError`, still a `ValueError` subclass) instead of a bare `ValueError` when a new config would change the immutable `worker`. Catch `SettingsValidationError` or `GrelmicroError` to handle it.
* 💥 Rename the `ExternalConfig` `interval` parameter to `reload_interval`, tying the knob to the `reload()` verb it controls. Update `ExternalConfig(interval=...)` to `reload_interval=`.
* 💥 Rename the log dedup `ttl_seconds` field to `ttl`, matching the bare-noun duration convention used everywhere else (the cache `ttl`, lock durations). Update `DuplicateFilter(ttl_seconds=...)` and `DuplicateFilterConfig` to `ttl=`.
* 💥 Rename the `Trace` component symbols to the `Trace` stem so they match the component name. `TracingConfig`, `TracingError`, `TracingExporterType`, `TracingProcessorType`, `TracingSamplerType`, and `TracingSettingsValidationError` become `TraceConfig`, `TraceError`, `TraceExporterType`, `TraceProcessorType`, `TraceSamplerType`, and `TraceSettingsValidationError`. Update imports.
* 💥 Rename the `Log` component symbols to the `Log` stem so they match the component name. `LoggingConfig`, `LoggingError`, `LoggingBackendType`, `LoggingFormatType`, `LoggingLevelType`, `LoggingSerializerType`, `LoggingTimeZoneType`, and `LoggingSettingsValidationError` become `LogConfig`, `LogError`, `LogBackendType`, `LogFormatType`, `LogLevelType`, `LogSerializerType`, `LogTimeZoneType`, and `LogSettingsValidationError`. Update imports.
* 💥 Rename the `TaskLock` lease boundaries to lease-anchored names, matching `LockConfig` and the Kubernetes `LeaseDuration`. `max_lock_seconds` becomes `lease_duration` and `min_lock_seconds` becomes `min_hold_duration`. This covers the `TaskLockConfig` fields and the `TaskLock` constructor. Update `TaskLock(max_lock_seconds=..., min_lock_seconds=...)` to `lease_duration=..., min_hold_duration=...` and the env vars `GREL_TASKLOCK_{NAME_UPPER}_MAX_LOCK_SECONDS` and `_MIN_LOCK_SECONDS` to `_LEASE_DURATION` and `_MIN_HOLD_DURATION`.
* 💥 Rename the concrete leader election adapters to the `*Adapter` suffix every other pattern already uses. `MemoryLeaderElectionBackend`, `RedisLeaderElectionBackend`, `PostgresLeaderElectionBackend`, and `KubernetesLeaderElectionBackend` become `MemoryLeaderElectionAdapter`, `RedisLeaderElectionAdapter`, `PostgresLeaderElectionAdapter`, and `KubernetesLeaderElectionAdapter`. The `LeaderElectionBackend` protocol keeps its name (protocol stays `*Backend`, concrete stays `*Adapter`). Update direct imports and constructions.
* 💥 Rename the resilience component wrappers to the singular `*Registry` form, matching their singular `kind` and the `Coordination` and `Cache` siblings. `RateLimiters` becomes `RateLimiterRegistry` and `CircuitBreakers` becomes `CircuitBreakerRegistry`. Update `uses=[...]` and imports.
* 💥 Drop the redundant `DEFAULT` segment from the default instance env prefix. A default instance (`Lock("default")`, `Retry.exponential("default")`, and the like) now reads the bare `GREL_{COMPONENT}_{FIELD}` instead of `GREL_{COMPONENT}_DEFAULT_{FIELD}`. Rename env vars like `GREL_LOCK_DEFAULT_LEASE_DURATION` to `GREL_LOCK_LEASE_DURATION`. Named instances are unchanged. The default instance now owns the bare `GREL_{COMPONENT}_` namespace, so name your other instances to avoid clashing with a field name (a `Lock("lease")` would share `GREL_LOCK_LEASE_DURATION` with the default instance).
* 💥 Raise `OutOfContextError` with an actionable message on every ambient backend miss: `Lock`, `TaskLock`, `LeaderElection`, `TTLCache`, `@cached`, the cron schedule resolution, and `Idempotency` now match `CircuitBreaker` and `RateLimiter`. `NoActiveAppError` stays the low-level error raised by `Grelmicro.current()` itself.
* 💥 Remove the implicit memory fallback on `CircuitBreaker` and `RateLimiter`. Backend resolution is now one rule on every pattern: explicit `backend=` wins, else the active app's component, else `OutOfContextError`. For a per-process limiter or breaker without an app, pass `backend=MemoryRateLimiterAdapter()` or `backend=MemoryCircuitBreakerAdapter()` (both import from `grelmicro.resilience`). Inside FastAPI handlers, add `GrelmicroMiddleware` so ambient resolution works there. `RateLimiter.reconfigure` now publishes the config and rebinds the strategy lazily on the next call, matching `CircuitBreaker`.
* 💥 Add positional argument capture to `grelmicro.testing`: `Call` is now `Call(method, args=..., kwargs=...)` and `CallLog.count` matches positional arguments too. Update direct `Call(...)` constructions.
* 💥 Align the leader election and task lock env var prefixes with their single-token names: `GREL_LEADER_ELECTION_{NAME}_` becomes `GREL_LEADERELECTION_{NAME}_` and `GREL_TASK_LOCK_{NAME}_` becomes `GREL_TASKLOCK_{NAME}_`. Update any environment variables set for these components. PR [#346](https://github.com/grelinfo/grelmicro/pull/346).
* 💥 Rename the pattern factory methods so each uses the pattern's single-token name: `Provider.breaker()` becomes `circuitbreaker()`, `leader_election()` becomes `leaderelection()` on both `Provider` and `Coordination`, and `Coordination.task_lock()` becomes `tasklock()`. This matches the module names (`grelmicro.coordination.leaderelection`, `grelmicro.coordination.tasklock`, `grelmicro.resilience.circuitbreaker`) and the `ratelimiter`/`circuitbreaker` kind strings. Update `provider.circuitbreaker()`, `micro.coordination.leaderelection(...)`, and `micro.coordination.tasklock(...)` call sites. PR [#343](https://github.com/grelinfo/grelmicro/pull/343), PR [#344](https://github.com/grelinfo/grelmicro/pull/344).
* 💥 Make `Log`, `Trace`, and `Metrics` singletons. Each configures process-global state (the root logger, the OpenTelemetry tracer and meter providers), so registering a second one on the same app now raises `ComponentAlreadyRegisteredError` instead of silently clobbering the first. PR [#343](https://github.com/grelinfo/grelmicro/pull/343).
* 💥 Make the component `name` a read-only property everywhere (`Coordination`, `Cache`, `Log`, `Trace`, `Metrics`, `RateLimiters`, `CircuitBreakers`, `HealthChecks`, `RealClock`, `VirtualClock`), matching the resilience and coordination primitives. Pass `name=` at construction. PR [#343](https://github.com/grelinfo/grelmicro/pull/343).

### Features

* ✨ Trace FastStream messages. `micro.install(faststream_app)` now wires the broker's OpenTelemetry telemetry middleware against the app's tracer, so consumed and published messages get spans with no per-handler decoration. Selected by the same `Trace(instrument=...)` directive under the `faststream` name, and a no-op when the broker's faststream telemetry support is not installed. ([#470](https://github.com/grelinfo/grelmicro/issues/470))
* ✨ Trace Valkey commands. grelmicro ships a first-party `ValkeyInstrumentor` (valkey-py has no official OpenTelemetry package), registered as a standard `opentelemetry_instrumentor` entry point so `Trace(instrument=...)` discovers it like any other library, and `ValkeyProvider` uses it. It reuses the Redis span factories against the `valkey.*` classes, so Valkey spans match the Redis ones. ([#479](https://github.com/grelinfo/grelmicro/issues/479))
* ✨ Trace any library the app uses, not just grelmicro-managed providers. `Trace(instrument=True)` now sweeps every installed `opentelemetry-instrumentation-*` package and attaches it to the app's tracer, so an app's own SQLAlchemy or asyncpg engine, httpx client, and the like are traced with no grelmicro provider. The set of installed instrumentors defines coverage (no new hard dependency), names follow the OpenTelemetry instrumentor names, and the asyncpg/SQLAlchemy pair is de-duplicated to avoid double spans. ([#479](https://github.com/grelinfo/grelmicro/issues/479))
* ✨ Fail fast on a missing ambient binding. `micro.install(app, ambient=False)` now warns at startup when ambient-resolving components are registered (it raises `AmbientBindingError` under `Grelmicro(strict=True)`), and the new `micro.check_ambient_binding(app)` returns whether the binding middleware is wired so a test can catch a forgotten `micro.install(app)` before it 500s on the first request. ([#471](https://github.com/grelinfo/grelmicro/issues/471))
* ✨ Auto-disable `Trace` until an endpoint is configured. The exporter now defaults to `TraceExporterType.AUTO`, which exports over OTLP HTTP when an endpoint is set (the `endpoint` argument, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, or `OTEL_EXPORTER_OTLP_ENDPOINT`) and no-ops otherwise. Register `Trace()` unconditionally and it stays silent in dev, test, and CI instead of falling back to `localhost:4318`. ([#476](https://github.com/grelinfo/grelmicro/issues/476))
* ✨ Add first-class HTTP Basic auth to `Trace`. Pass `basic_auth=(username, password)` or set `GREL_TRACE_BASIC_AUTH_USERNAME` and `GREL_TRACE_BASIC_AUTH_PASSWORD`, and the `Authorization: Basic` header is built and attached to the exporter directly, bypassing the fragile `OTEL_EXPORTER_OTLP_HEADERS` encoding. ([#476](https://github.com/grelinfo/grelmicro/issues/476))
* ✨ Add native auto-instrumentation to `Trace`. `Trace(instrument=...)` traces incoming FastAPI requests and Redis and Postgres calls against the app's tracer provider, no per-handler decoration. On by default, a no-op until the new `instrumentation` extra is installed. Pass `False`, a name or list to select, or a `{name: False}` map to exclude. Redis attaches per-client, asyncpg is patched process-wide, and Valkey and SQLite stay on `@instrument`.
* ✨ Add `RateLimiter.wait()`, a blocking admission verb that waits until tokens are available then consumes them. It polls on the clock seam (so `VirtualClock` drives it in tests), waits as long as needed by default, and raises `RateLimitExceededError` once an optional `max_wait` budget is exceeded. A `cost` larger than the limit raises `ValueError` instead of waiting forever.
* ✨ Add `LeaderElection.lead(func, *, repeat=False)`, which runs a coroutine only while the worker holds leadership and cancels it the instant leadership is lost, so no stale work outlives the lease. It returns the body's result if it finishes while still leader, or `None` if cancelled. Pass `repeat=True` to re-run after re-acquiring leadership.
* ✨ Add `micro.install(app)`, one call that wires the lifecycle and per-handler ambient binding for Starlette, FastAPI, and FastStream. Pass `ambient=False` to skip the binding.
* ✨ Accept a `timedelta` for the interval `seconds=`, like `@task.every(seconds=timedelta(minutes=2))`. A plain number of seconds still works.
* ✨ Add a `key=` template to `@cached` for a stable, readable cache key rendered from the arguments, like `@cached(key="user:{user_id}")`. Pass `key_maker=` for the fully dynamic case. Passing both raises `TypeError`.
* ✨ Type `FireInfo.outcome` as the new `FireOutcome` `StrEnum` (`SUCCESS`, `ERROR`, `SKIPPED`). String comparisons like `outcome == "success"` still work.
* ✨ Add `Idempotency.run(key, factory)`, a one-call helper that runs an operation once and replays its response. It takes a sync or async factory and mirrors `TTLCache.get_or_set`.
* ✨ Every component now raises a typed `*SettingsValidationError` for invalid configuration, rooted in the shared `SettingsValidationError` base. Adds `TraceSettingsValidationError`, `HealthSettingsValidationError`, `LogSettingsValidationError`, and `IdempotencySettingsValidationError`. Catch `SettingsValidationError` to handle any of them.
* ✨ Cache adapters (`MemoryCacheAdapter`, `RedisCacheAdapter`, `PostgresCacheAdapter`, `SQLiteCacheAdapter`) now declare the `CacheBackend` protocol explicitly, matching the lock, circuit breaker, and rate limiter adapters.
* ✨ Add `Log.from_config`, `Trace.from_config`, and `Metrics.from_config` to build each component from a pre-built config, matching the declarative path on every other pattern. The `config=` kwarg still works.
* ✨ Add `MemoryProvider` so Memory has the same provider-direct surface (`memory.lock()`, `memory.cache()`, ...) as Redis, Postgres, and SQLite.
* ✨ Add a built-in readiness check per provider. Every connection provider ships a cheap `check()` probe (Redis and Valkey `PING`, Postgres and SQLite `SELECT 1`). Register it with `health.add_provider(redis)` as a critical `provider:redis` check, or register one for every active provider at once with `HealthChecks(auto_health=True)`. `Grelmicro.providers` lists the active providers.
* ✨ Default the rate limiter `key` to `"default"` on `acquire`, `acquire_or_raise`, `allow`, `peek`, and `reset`, so the single-bucket case is `await limiter.allow()`. The limiter `name` already namespaces the backend key.
* ✨ Add the zero-object `@cached(ttl=30)` form for plain memoization: it binds a private process-local `TTLCache` at decoration, never resolves the active app, and never shares across replicas. Pass a `TTLCache` for shared state. Passing both `cache` and `ttl` raises `TypeError`.
* ✨ Add the OpenSSF Scorecard workflow and badge.
* ✨ Make `Grelmicro(uses=[...])` and `micro.use(...)` forgiving: a bare Component class is constructed for you, a bare adapter (class or instance) is wrapped in its matching Component, and a bare Provider with no Components auto-registers one default Component per kind it serves. The explicit form always wins, so any explicit Component turns provider auto-registration off entirely.
* ✨ Add `AmbiguousProviderError`, raised when `uses=[...]` lists two bare Providers with no Components, so the default Component for a shared kind would be ambiguous. Wrap each Provider in the Components it should serve to resolve it.
* ✨ Add the Idempotency pattern: a new `grelmicro.idempotency` module with an `Idempotency` primitive and `@idempotent` decorator. A caller-provided key (an `Idempotency-Key` header) executes the operation once, stores the response for `ttl` seconds, and replays it on repeats. Duplicates arriving mid-flight fold into the first execution, across replicas when a Coordination lock backend is configured. A failure stores nothing, so a retry executes fresh. An optional `fingerprint=` rejects a reused key with a different payload via `IdempotencyConflictError`. Storage rides the cache layer (`cache=` or the active app's `Cache` component).
* ✨ Add Redis Sentinel and Redis Cluster support: `redis+sentinel://host1:26379,host2:26379/service` and `redis+cluster://host1,host2` URL schemes on `RedisProvider`, plus `RedisProvider.sentinel(...)` and `RedisProvider.cluster(...)` factories, so one URL switches topology. On Cluster, the multi-key cache and lock operations require a hash-tagged prefix (`prefix="{app}cache"`), enforced with a clear error at construction.
* ✨ Add Valkey support: a `ValkeyProvider` in `grelmicro.providers.valkey` (extra `valkey`) serves the full Redis adapter column (Lock, TaskLock, LeaderElection, Schedule, TTLCache, RateLimiter, CircuitBreaker) through the `valkey` client.
* ✨ Add the Externalized Configuration pattern: a new `grelmicro.config` module with an `ExternalConfig` component that reconfigures live components from a mounted ConfigMap, Secret, `.env`, JSON, YAML, or TOML file (`FileConfigAdapter`, nested mappings flatten to env-style keys), polling on an interval with a public `reload()` for an immediate pass. Sources are pluggable via the `ConfigBackend` protocol. Every named pattern built imperatively registers under its `GREL_{PATTERN}_{NAME}_` keys, including `CircuitBreaker` (`GREL_CIRCUITBREAKER_{NAME}_`) and `RateLimiter` (`GREL_RATELIMITER_{NAME}_`). Instances built from a pre-built config stay static. Validation warnings log field names only, never values.
* ✨ Add `GrelmicroMiddleware` in `grelmicro.fastapi`: a pure ASGI middleware that binds the active app inside request handlers, so `Lock("cart")`, `RateLimiter.sliding_window(...)`, and `@cached` resolve ambiently in handlers without explicit `backend=` wiring.
* ✨ Add a bounded wait to `Lock.acquire(timeout=)`, raising `TimeoutError` at the deadline, and `Lock.extend()` to renew the lease of a held lock without releasing it. Both are mirrored on the `from_thread` facade.
* ✨ Add `TaskLock.refresh()` so a task body that may outrun `max_lock_seconds` can renew its claim, raising `LockNotOwnedError` when the claim was lost.
* ✨ Add `retry_jitter` to `Lock` and `LeaderElection` (default 0.1): each retry sleeps `retry_interval * uniform(1 - jitter, 1 + jitter)`, so contending workers spread their attempts instead of retrying in lockstep.
* ✨ Add scheduler introspection: `next_fire_time` and `last_fire` on interval and cron tasks, with `FireInfo` (started_at, outcome, duration) exported from `grelmicro.task`.
* ✨ Add `Match.explain()` returning the human-readable matcher tree, and warn once when a predicate returns a non-bool value.
* ✨ Add a shared `AdmissionError` base so every gatekeeping rejection is catchable with one `except`. `RateLimitExceededError`, `BulkheadFullError`, `CircuitBreakerError`, and `WouldBlockError` now inherit it, so `except AdmissionError` handles a rate limiter over budget, a full bulkhead, an open circuit breaker, or a non-blocking lock that would block. It is purely additive: the existing per-primitive `except` clauses still work. PR [#354](https://github.com/grelinfo/grelmicro/pull/354).
* ✨ Add `RateLimiter.allow(key=...)` returning a `bool` for the common served-or-throttled branch, and make `RateLimitResult` truthy (`bool(result)` is `result.allowed`). `if await limiter.acquire(key=...):` now reads as the decision while `retry_after` and `remaining` stay available on the result. PR [#354](https://github.com/grelinfo/grelmicro/pull/354).
* ✨ Add serve-stale-on-error to the cache with `stale_ttl=` on `@cached`, `get_or_set`, and `TTLCache.set`. Each value keeps a fallback copy for `ttl + stale_ttl` seconds, so a recompute that fails after the TTL serves the last good value instead of raising, up to `stale_ttl` seconds late. A flaky upstream degrades to slightly stale data instead of an error storm. It composes with `lock` and `early`, an explicit delete or tag invalidation drops the fallback, and each stale serve records the `grelmicro.cache.stale_serves` metric. PR [#350](https://github.com/grelinfo/grelmicro/pull/350).
* ✨ Add SQLite cache and circuit breaker backends, completing the SQLite column of the capability matrix (the circuit breaker coordinates single-host multi-process state). PR [#349](https://github.com/grelinfo/grelmicro/pull/349).
* ✨ Add a durable `@tasks.cron(expr, timezone="UTC")` decorator that runs a task on a 5-field cron schedule (`minute hour day-of-month month day-of-week`). The parser is built in, with no external dependency, and supports `*`, steps, ranges, lists, and the `7`-as-Sunday alias. It uses standard Vixie day-of-month/day-of-week OR semantics. Each fire is claimed against a durable last-fire state via a new `ScheduleBackend` (Memory, Redis, Postgres, and SQLite), so the task runs at most once across all workers per fire. A fire missed while every worker was down replays once on restart, bounded by `misfire_grace_seconds`, and only the most recent missed fire runs. Wire it via `Coordination(provider)` or `Coordination(schedule=...)`. PR [#348](https://github.com/grelinfo/grelmicro/pull/348).
* ✨ Add a time-based stop to `Retry` with `max_seconds=`. Retrying stops as soon as either `attempts` is reached or the wall-clock budget elapses, whichever comes first (`attempts` still defaults to 3). Available on the `Retry.exponential`/`Retry.constant` factories, the constructor, and `RetryConfig` (env var `GREL_RETRY_{NAME}_MAX_SECONDS`). The budget reads the clock seam, so `VirtualClock` drives it in tests. PR [#347](https://github.com/grelinfo/grelmicro/pull/347).
* ✨ Re-export `FunctionTypeError` and `TaskAddOperationError` from `grelmicro.task`, so the task errors users catch live next to `TaskError` instead of only in `grelmicro.task.errors`. PR [#343](https://github.com/grelinfo/grelmicro/pull/343).
* ✨ Export the catch-all base `GrelmicroError` and the cross-cutting `DependencyNotFoundError`, `OutOfContextError`, and `SettingsValidationError` from the top-level `grelmicro` package, so `except GrelmicroError` catches any library error from one import. Re-export `WouldBlockError` and `CoordinationBackendError` from `grelmicro.coordination` (the latter moved into `grelmicro.coordination.errors`). PR [#343](https://github.com/grelinfo/grelmicro/pull/343).

### Fixed

* 🐛 Name the failing source in the `ExternalConfig` reload warning, so a broken config or secrets mount is no longer a generic warning. Each source loads under its own guard, so a config failure no longer hides a working secrets source. Source values are never logged.
* 🐛 Raise an actionable `RuntimeError` from a sync `@cached` call when the backend never captured a running loop, instead of an opaque `AttributeError`. The message says to open the backend with `async with micro:` first.
* 🐛 Reconcile cache tags on every Redis `set` and `set_many`, even with no tags. Re-setting a previously tagged key without tags now drops its stale tag membership, so a later `delete_tags` no longer wrongly removes it. PR [#353](https://github.com/grelinfo/grelmicro/pull/353).
* 🐛 Store the cache sidecar entries (the `early=` refresh metadata, and the new stale reserve) under a `\x1f` separator instead of `\x00`, so they are valid Postgres text keys. `@cached(early=...)` previously raised on a Postgres cache backend. PR [#350](https://github.com/grelinfo/grelmicro/pull/350).

### Docs

* 📝 Add `docs/architecture/decorators.md` documenting the bare `@deco` versus parametrized `@deco(...)` rule and which decorators wrap sync functions. PR [#343](https://github.com/grelinfo/grelmicro/pull/343).

## 0.27.0 - 2026-06-07

### Breaking

* 💥 Replace `@cached(stampede="local" | "distributed" | None)` with `@cached(lock=False | True | "local")`. `lock=True` folds concurrent misses and picks the cross-replica path automatically when the active app has a `Coordination` lock backend (in-process otherwise), `lock="local"` forces the in-process path, and the default is now `lock=False` (no protection, opt in explicitly). Migrate `stampede="local"` to `lock="local"`, `stampede="distributed"` to `lock=True`, and `stampede=None` to `lock=False`. Issue [#235](https://github.com/grelinfo/grelmicro/issues/235).
* 💥 Move `LeaderElection` out of `grelmicro.sync` into a new `grelmicro.coordination` package. Import it from `grelmicro.coordination`. `Sync.leader_election()` is removed: register a `Coordination` component and call `micro.coordination.leader_election(...)`. Leader election now runs on a dedicated `LeaderElectionBackend`, not the lock `SyncBackend`, so it can use a different vendor than `Lock` (Redis for `Lock`, a Kubernetes Lease for leader election). Issue [#223](https://github.com/grelinfo/grelmicro/issues/223).
* 💥 Unify `grelmicro.sync` into `grelmicro.coordination` and delete `grelmicro.sync`. Import `Lock`, `TaskLock`, `LeaderElection`, and `Coordination` from `grelmicro.coordination`. The `Sync` component is gone: use one `Coordination` component, which exposes `.lock(...)`, `.task_lock(...)`, and `.leader_election(...)`, and reach it on `micro.coordination`. The `SyncBackend` protocol is now `LockBackend`, the `*SyncAdapter` backends are now `*LockAdapter`, and the provider factory `.sync()` is renamed to `.lock()`. Issue [#223](https://github.com/grelinfo/grelmicro/issues/223).
* 💥 Make the JSON utilities internal. The `grelmicro.json` module is removed. Use `JsonSerializer` from `grelmicro.cache` for cache JSON, or `orjson` directly if you need raw fast JSON.

### Features

* ✨ Add a `Metrics` component that installs an OpenTelemetry `MeterProvider` for the app's lifetime, with OTLP, Prometheus, console, and none exporters. A `@measure` decorator times and counts any function, `metrics_router()` serves a Prometheus `/metrics` endpoint, and every built-in component (health, circuit breaker, retry, rate limiter, bulkhead, timeout, cache, tasks) emits its own metrics. All metric calls are no-ops without the `opentelemetry` extra or an active component.
* ✨ Leader election leases carry a Kubernetes-style `LeaderRecord` (holder, lease duration, acquire and renew times, leadership transitions, and free-form metadata). Read it from `LeaderElection.record`, set the metadata via `LeaderElection(metadata=...)`. Metadata-storing backends ship for memory, Redis, Postgres, and Kubernetes Lease, resolved through `provider.leader_election()` or passed to `Coordination(...)` directly. Issue [#223](https://github.com/grelinfo/grelmicro/issues/223).
* ✨ Add `grelmicro.testing.record(backend)` for protocol-level call assertions. It instruments a backend's public async methods in place and returns a `CallLog`, so the backend keeps its type and behavior while every call is recorded. Assert with `log.count(method, **kwargs)`, inspect `log.methods()`, or read the raw `log.calls`. Works like `pytest-mock`'s `mocker.spy`. Issue [#271](https://github.com/grelinfo/grelmicro/issues/271).
* ✨ Add cache tags, `get_or_set`, and batch operations. Tag entries via `set`, `set_many`, `get_or_set`, or `@cached(tags=["users", "user:{user_id}"])`, then invalidate a whole group with `delete_tags`. `get_or_set(key, factory)` computes a missing value once under the same stampede protection as `@cached(lock=True)`. `get_many`, `set_many`, and `delete_many` work on many keys at once. Tags and batch ops run on Memory, Redis, and Postgres.
* 📝 Correct the comparison page and capability matrix to show the Postgres and SQLite cache, rate limiter, and circuit breaker backends as shipped (they were stale-labeled "planned").
* 📝 Add a "what grelmicro is not" line to the README and docs landing for sharper first-read positioning.
* 🔧 Set the PyPI `Development Status` classifier to `4 - Beta`.
* ✨ Discover Providers and Adapters through entry-point groups. Third-party packages register under `grelmicro.providers` and `grelmicro.{kind}.adapters` (`coordination`, `cache`, `ratelimiter`, `circuitbreaker`) and resolve by short name, the same path first-party backends use. Unknown names raise `ProviderNotRegisteredError` or `AdapterNotRegisteredError` listing the installed names. New `docs/architecture/plugins.md` and an `examples/third-party-adapter/` skeleton. Issue [#234](https://github.com/grelinfo/grelmicro/issues/234).
* ✨ Add `VirtualClock` for deterministic time in tests. Time-dependent primitives (`Retry` backoff, `CircuitBreaker` half-open window, `RateLimiter` refill, `Shield` adaptive gate) read time through a clock seam (`grelmicro.clock.monotonic` / `sleep`). Install a `VirtualClock` (`Grelmicro(uses=[clock, ...])` or `async with VirtualClock()`) and call `clock.advance(seconds)` to drive that behavior with no real waiting. With no clock registered, the seam forwards to `time.monotonic` and `asyncio.sleep`, so production keeps real time. Issue [#272](https://github.com/grelinfo/grelmicro/issues/272).
* ✨ Auto-discover shared Providers in `Grelmicro(uses=[...])`. A Provider held by a Component (`Coordination(redis)`, `Cache(redis)`) no longer has to be listed separately: it is adopted and lifecycled exactly once, opened before the Components that hold it. Listing it explicitly stays valid and keeps control over lifecycle order. Issue [#263](https://github.com/grelinfo/grelmicro/issues/263).

### Docs

* 📝 Lead every feature page with the simplest runnable example, then explain, moving deep theory into collapsible sections. Covers the resilience patterns and the cache, coordination, logging, health, tracing, and task guides.

## 0.26.0 - 2026-06-05

### Breaking

* 💥 The `Task` protocol's `__call__` now takes a `stop: asyncio.Event | None = None` keyword used for graceful shutdown. Custom `Task` implementations must accept it. The built-in `interval` tasks and `LeaderElection` are unaffected. Issue [#187](https://github.com/grelinfo/grelmicro/issues/187).
* 💥 Replace the `@cached(lock=...)` parameter with `@cached(stampede="local" | "distributed" | None)`. `lock=True` becomes `stampede="local"` (now the default), `lock=False` becomes `stampede=None`, and the custom-context-manager form is dropped in favor of the `"distributed"` cross-replica mode. Issue [#235](https://github.com/grelinfo/grelmicro/issues/235).

### Features

* 📝 Add a runnable FastAPI demo under `examples/fastapi-demo/`. `docker compose up --wait` starts Redis, Postgres, and a FastAPI app that exercises every Pattern (cache, rate limiter, circuit breaker, distributed lock, leader-gated task, health probes), with a `Demo Smoke` CI job and a `just demo` shortcut. Issue [#166](https://github.com/grelinfo/grelmicro/issues/166).
* 📝 Add a ConfigMap-watcher example wiring `reconfigure()` (`docs/configuration/reconfigure-from-configmap.md`), with a `SIGHUP` variant for non-Kubernetes hosts. Issue [#169](https://github.com/grelinfo/grelmicro/issues/169).
* ✨ Accept bare zero-arg classes in `Grelmicro(uses=[...])`, `micro.use(...)`, and the `Sync` / `Cache` / `RateLimiters` / `CircuitBreakers` constructors. `uses=[MemorySyncAdapter]` and `Sync(MemorySyncAdapter)` now work without the trailing `()`, in the spirit of FastAPI's `Depends(dep)`. A class that needs constructor arguments raises a clear error. Issue [#263](https://github.com/grelinfo/grelmicro/issues/263).
* ✨ Guard against two overlapping `Grelmicro` apps clobbering process-global state. Opening a second app that registers `Log` or `Trace` while another such app is active now raises `MultipleActiveAppsError`. Apps without those components overlap freely. Pass `Grelmicro(allow_multiple=True)` to opt out. New `docs/architecture/multiple-apps.md` documents the policy. Issue [#266](https://github.com/grelinfo/grelmicro/issues/266).
* ✨ Add `Tasks(shutdown_timeout=...)` for graceful shutdown. On exit, `Tasks` signals every `interval` task to finish its current run and stop, force-cancelling only stragglers that outlast the timeout. The default `30.0` matches Kubernetes' `terminationGracePeriodSeconds`, and `LeaderElection` releases leadership on the same signal. New `docs/architecture/graceful-shutdown.md` covers signal wiring. Issue [#187](https://github.com/grelinfo/grelmicro/issues/187).
* ✨ Add a three-layer cache stampede menu to `@cached`. `stampede="local"` (default) folds concurrent same-key misses to one in-process run, `stampede="distributed"` coordinates across replicas through the `Sync` component, and `early=` (XFetch) refreshes the hottest keys in the background before they expire so no caller blocks. Issue [#235](https://github.com/grelinfo/grelmicro/issues/235).
* ✨ Add `LeaderElection.last_confirmation_age()` (seconds since the last backend response that confirmed local leadership, `None` until first acquisition and after confirmed loss) and `LeaderElection.is_leader_confirmed_within(max_age)` (stricter variant of `is_leader()` that requires a recent backend renewal). The `is_leader()` docstring now spells out the advisory uncertainty window during a backend partition.
* ✨ Add `Grelmicro(strict=True)` to raise `LifecycleOrderError` instead of warning when a Component holds a Provider that is missing from `uses=` or listed after the dependent Component. The default `False` preserves the lenient warn-only behavior. `LifecycleOrderError` is exported from `grelmicro`.
* ✨ Add `Shield` resilience pattern: per-attempt timeout, retry-budget-gated retries, CUBIC-style adaptive rate limiter, optional cache and fallback recovery paths. Three profiles (`internal`, `api`, `slow`) cover the common cases. Decorator (`@shield`, `@shield.api(...)`), class (`Shield.api("name")`), and imperative (`Shield.api("name").run(fn, ...)`) forms supported. Issue [#249](https://github.com/grelinfo/grelmicro/issues/249).
* ✨ Add `TTLCacheConfig` and expose it via `TTLCache.config`. Matches the frozen-config shape used by every other primitive.
* ✨ Add `RedisProvider.safe_url` and `PostgresProvider.safe_url` returning the resolved URL with the password replaced by `***`. The new `__repr__` on both providers uses the safe form so credentials never leak through logs or tracebacks.
* ✨ Add `TracingConfig.shutdown_timeout` (default `5.0` seconds). `Trace.__aexit__` now runs `TracerProvider.shutdown()` in a thread with this deadline so a slow or broken exporter no longer hangs application shutdown.
* ✨ Add `SQLiteProvider` and SQLite rate limiting. Use `RateLimiters(SQLiteProvider("app.db"))` for file-backed limits on a single host. Each acquire runs a read-modify-write inside a `BEGIN IMMEDIATE` transaction. Issue [#173](https://github.com/grelinfo/grelmicro/issues/173).
* ✨ Add `PostgresCircuitBreakerAdapter` for fleet-wide circuit breaker state on Postgres, plus `PostgresProvider.breaker()` so `CircuitBreakers(postgres)` resolves it. Transitions run in PL/pgSQL functions guarded by `pg_advisory_xact_lock`.
* ✨ Add `Bulkhead` resilience pattern to cap concurrent in-flight calls. `max_concurrent` bounds concurrency, `max_wait` lets callers queue briefly before a `BulkheadFullError` (default fails fast), and `max_workers` runs blocking work through `bulkhead.to_thread` on a dedicated pool. Async context manager and decorator forms. Issue [#168](https://github.com/grelinfo/grelmicro/issues/168).
* ✨ Add `Bulkhead(uses=[...])` to scope Providers and Components to a bulkhead. Inside the scope, a Pattern resolving its default backend picks up the bulkhead's Component, isolating a business context onto its own pool. Explicit `backend=` still wins. Issue [#168](https://github.com/grelinfo/grelmicro/issues/168).

### Fixes

* 🐛 The README and `simple_fastapi_app.py` FastAPI examples now pass an explicit `backend=` to patterns used inside request handlers. Request handlers run outside the app's ambient `Grelmicro.current()` scope, so the previous ambient form raised `NoActiveAppError` (locks, cache) or silently fell back to an in-memory backend (rate limiter, circuit breaker) at runtime. Background `Tasks` keep using ambient resolution. Ambient resolution in handlers is tracked in [#328](https://github.com/grelinfo/grelmicro/issues/328).
* 🔒 `SettingsValidationError` no longer echoes the offending input value. Env-loaded credentials (DSNs, tokens) no longer surface in error messages.
* 🚨 `ComponentNotRegisteredError` from `Grelmicro.get(kind, name)` now lists every registered `(kind, name)` pair (or states that none are registered). Agents and developers see what is available without inspecting the container.
* 🚨 `HealthChecks.add` invalid-name errors now include valid examples (`'redis'`, `'db-primary'`, `'weather:circuitbreaker'`) alongside the regex.
* 🐛 `Log.__aenter__` and `Log.__aexit__` now serialize on a class-level `threading.Lock` so concurrent `Grelmicro` lifecycles in the same process cannot interleave the stdlib root-logger snapshot / restore sequence.
* 🐛 Unexpected exceptions inside a health check now surface as `"TypeName: message"` in the `CheckResult.error` field instead of the generic `"Health check failed"`. Operators reading only the `/healthz` payload can identify the failing class without grepping logs.
* 🔒 `Lock("...")` now validates the name against `^[A-Za-z0-9][A-Za-z0-9._:/-]*$` (max 200 chars). Names with whitespace, control characters, or leading separators are rejected with a message that includes valid examples. Existing namespaced names (`users:42`, `payments/eu`, `weather.svc`) keep working.
* ⚡ `DuplicateFilter` now sweeps entries older than `ttl_seconds` once per window, so high-cardinality log floods stop evicting still-active keys by size pressure.

### Docs

* 📝 Lead `README.md` and `docs/index.md` with a one-route, one-primitive FastAPI example before the full composition demo.
* 📝 Annotate `Grelmicro.use`, `Grelmicro.get`, `instrument`, and `CacheBackend` protocol parameters with `Annotated[..., Doc(...)]`.
* 📝 Align the `CONTRIBUTING.md` discriminator rule with the code: `kind` (not `type`).
* 📝 Document the per-process scope of `Tasks` and point at `TaskLock` / `LeaderElection` for cluster-wide scheduling.
* 📝 Add a Kubernetes operational-assumptions section covering RBAC, API server availability, etcd latency, and single-cluster scope to `docs/architecture/kubernetes.md`.
* 📝 Fix the `sync.md → task.md#tasks` internal anchor so `mkdocs --strict` no longer reports it.
* 📝 Add a lifespan-only example (one provider, one component) between the minimal example and the full composition demo in `README.md` and `docs/index.md`.
* 📝 Drop unsupported claims and idioms from the landing copy ("Stop reinventing the wheel", "battle-tested in production", "TL;DR").
* 📝 Add `Start here` / `Common recipes` lead lines to every page under `docs/reference/`.
* 📝 Add an explicit `Running tests` section to `CONTRIBUTING.md` with the commands for unit-only, integration-only, and the full local gate.
* 📝 Add a `What should I pick?` decision tree to the top of `docs/comparison.md` so readers can map their situation to the right tool (one primitive, two or more, task queue, workflow engine, web framework).
* 📝 Add a `Your first contribution` section to `CONTRIBUTING.md` with the expected code, test, and docs shape and a pointer to the `good first issue` label.
* 📝 Add `Annotated[..., Doc(...)]` to the `SyncBackend`, `RetryStrategy`, `RateLimiterStrategy`, `RateLimiterBackend`, `CircuitBreakerStrategy`, and `CircuitBreakerBackend` protocol parameters so IDE and LLM tools surface the same hints on backends as on user-facing primitives.
* 📝 Group the `grelmicro.resilience` package docstring into front doors, components, adapters, and configs so import-site hover help guides agents and humans to the preferred entry point.
* 📝 Document that auto-generated task references (`module:qualname`) surface in logs, distributed lock keys, and metric labels. Suggest passing an explicit `name=` for sensitive workflows in `validate_and_generate_reference` and the `docs/task.md` Interval Task section.
* 📝 Add a `Why Python 3.12` section to `docs/installation.md` listing the language features (PEP 695, `asyncio.timeout`) that drive the floor, and note that CI runs the matrix on every advertised classifier (3.12, 3.13, 3.14).
* 📝 Add a `Platforms` column to the Optional extras table in `docs/installation.md` calling out that `uvloop` is skipped on Windows and PyPy.
* 📝 Document `RateLimitResult.remaining` as an estimate for continuous-state algorithms (GCRA-based sliding window). Enforcement still uses exact state, so the next `acquire` may be denied even when `remaining > 0`.
* 📝 Add a FastStream resilience recipe (`docs/snippets/resilience/faststream.py`) that uses a fleet-wide per-key `Lock` and a sliding-window `RateLimiter` inside a Redis-broker subscriber. Linked from `docs/resilience/index.md`.
* 📝 Formalize the `test_<component>_<scenario>_<expected_outcome>` test-name shape in `CONTRIBUTING.md` with three concrete examples.
* 📝 Add `docs/benchmarks.md` with reproducible request-path benchmarks for the rate limiter, circuit breaker, cache, and lock, plus runnable scripts under `benchmarks/`.
* 📝 Add a `Choosing a backend` guide to the sync, cache, rate limiter, and circuit breaker pages.
* 📝 Expand `docs/json.md` with supported types, the orjson fallback, and serializer boundaries.
* 📝 Note that the default OTLP HTTP trace exporter expects a running collector in `docs/tracing.md`, with `CONSOLE` and `NONE` for local development.

### Internal

* 🔒 `@instrument` now filters arguments whose names match common secret keywords (`password`, `token`, `secret`, `api_key`, `authorization`, `cookie`, ... matched case-insensitively) from both span attributes and log context. Pass extra names via `skip=` for custom secret-bearing parameters. Unchanged for non-sensitive args.
* 🔧 Replace the optional `orjson` redef-as-`Any | None` pattern in `grelmicro/_json.py` with try/except branches that define the dumps/loads functions in scope. The per-call `# type: ignore[union-attr]` directives are gone, and `orjson` keeps its real type from the stub package in the available branch.
* 🚨 `Trace.__aenter__` now raises `TracingError` if `opentelemetry.trace._TRACER_PROVIDER` is missing instead of silently no-op patching. A future OTel that drops the private global surfaces a clear error pointing at the workaround. An inline comment near the patch documents why the private attribute is required.
* 🔒 `PickleSerializer` docs upgraded to a Danger callout. Pickle is now framed as trusted in-process backends only, and the `@cached` decorator example leads with `JsonSerializer`. The `TTLCache` docstring lists Pydantic and JSON before Pickle.
* 🔧 Comment why `_env_prefix=env_prefix` needs a type-ignore in `RedisProvider` and `PostgresProvider` (pydantic-settings runtime kwarg the stubs do not expose).
* ⚡ Snapshot hot config fields (`cost`, `allowed_repetitions`, `ttl_seconds`, `cache_size`) onto `RateLimitFilter` and `DuplicateFilter` instances during setup so the per-record `filter()` path reads plain attrs instead of walking the Pydantic config.
* 🔧 Drop three unused `ty: ignore` directives in `grelmicro/_json.py`.
* ⚡ `@cached(lock=True)` per-key lock dictionaries now bound their size with LRU eviction (1024 entries). High-cardinality miss-heavy workloads no longer accumulate `asyncio.Lock` / `threading.Lock` objects indefinitely. Held locks are never evicted, so in-flight stampede protection is preserved.
* 🔒 `PostgresRateLimiterAdapter` advisory locks now use `pg_advisory_xact_lock(hashtextextended(key, namespace))`. The grelmicro-specific seed gives rate-limiter keys their own 64-bit lock-id space, isolating them from any other advisory lock in the same database and reducing intra-rate-limiter collisions from a 32-bit birthday risk to a 64-bit one.
* ✅ Add a `tests/typing/` sample (`test_cache_generics.py`) that uses `typing.assert_type` to lock in `TTLCache[T]`, `PickleSerializer[T]`, and `PydanticSerializer[T]` inference end-to-end. A regression that widens inference back to `Any` fails `uv run ty check`.
* ✅ Add a guard test that every `_LAZY` key in `grelmicro/resilience/__init__.py` is exported in `__all__` and actually resolves at runtime.
* ✅ Add Hypothesis property tests for token-bucket and sliding-window math and for exponential backoff jitter bounds.
* ✅ Enable branch coverage (`--cov-branch`). The 100% gate now covers both lines and branches. Defensive guards against impossible state are marked with `# pragma: no branch`.
* 🔧 Document why every `type: ignore` and `ty: ignore` in `grelmicro/_config.py` is required (Pydantic dynamic-subclass boundary).
* 🔧 Explain the double-checked `pragma: no cover` in `Reconfigurable.reconfigure` so future contributors see the concurrent-caller intent.
* 🔧 Add inline attribution cues to `grelmicro/task/_utils.py` and `grelmicro/resilience/_protocol.py` / `grelmicro/cache/_protocol.py` so readers immediately see where third-party adaptations live and that protocol bodies live in concrete adapters.
* 🔧 Fix the `THIRD_PARTY_NOTICES.md` path to `grelmicro/resilience/ratelimiter/redis.py`.

## 0.25.0 - 2026-05-21

### Features

* ✨ Add `Timeout` reconfigurable resilience pattern. `Timeout("db", seconds=2.0)` wraps `asyncio.timeout`, usable as an async context manager (`async with db_timeout:`) or decorator on async functions. `TimeoutConfig` is a frozen three-paths Pydantic config with `seconds: PositiveFloat`. Env prefix `GREL_TIMEOUT_{NAME_UPPER}_`. Inherits `Reconfigurable[TimeoutConfig]` for live deadline swaps. Issue [#176](https://github.com/grelinfo/grelmicro/issues/176).
* ✨ Add `Fallback` primitive with decorator, block, and class forms. `@fallback(when=..., default=...)` / `@fallback(when=..., factory=...)` swap a matched exception for a safe value. `async with falling_back(when=..., default=...) as result:` covers inline blocks. `Fallback("name", when=..., default=...)` is the named, reconfigurable class form. `FallbackConfig` is a frozen three-paths Pydantic config with `default` / `factory` mutually exclusive. `when=` matches Retry's keyword so the `Match` DSL stays universal. Composition order documented in [Composing patterns](resilience/composition.md). Issue [#199](https://github.com/grelinfo/grelmicro/issues/199).
* ✨ Add `PostgresCacheAdapter` for Postgres-backed cache storage. Register via `Grelmicro(uses=[postgres, Cache(postgres)])`. Entries land in a single `grelmicro_cache` table keyed on `key TEXT PRIMARY KEY` with `value BYTEA` and `expires_at TIMESTAMPTZ`. `get` filters expired rows with `WHERE expires_at > NOW()`, `set` is one `INSERT ... ON CONFLICT DO UPDATE`. Schema auto-migrates on first connect, opt out with `auto_migrate=False`. Optional janitor reclaims storage when `cleanup_interval=` is set (off by default). Issue [#167](https://github.com/grelinfo/grelmicro/issues/167).
* ✨ Add `PostgresRateLimiterAdapter` for fleet-wide rate limiting on Postgres. Register via `Grelmicro(uses=[postgres, RateLimiters(postgres)])` and `RateLimiter.token_bucket(...)` or `RateLimiter.sliding_window(...)` runs against a single `grelmicro_rate_limiter` table. `acquire` and `peek` each run one round-trip to a PL/pgSQL function. Concurrent writes for the same key are serialized with `pg_advisory_xact_lock`. Schema and functions auto-migrate on first connect, opt out with `auto_migrate=False`. Issue [#164](https://github.com/grelinfo/grelmicro/issues/164).

## 0.24.0 - 2026-05-18

### Features

* ✨ Add `CircuitBreakerStrategy` Protocol and `CircuitBreakerBackend.bind(name, config) -> Strategy`. Mirrors the RateLimiter shape so a second algorithm plugs in without breaking changes. `CircuitBreakerConfig` gains a `kind: Literal["consecutive_count"]` discriminator. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* ✨ Add `RedisCircuitBreakerAdapter` for fleet-wide breaker state. Register via `Grelmicro(uses=[redis, CircuitBreakers(redis)])` and `CircuitBreaker("name")` consults Redis for admission, counters, and transitions. Half-open admission cap is enforced globally via atomic Lua scripts. `last_error` and `last_error_time` stay per-replica. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* ✨ Add `CircuitBreaker.consecutive_count(name, ...)` factory classmethod, mirroring `RateLimiter.token_bucket(...)` and `RateLimiter.sliding_window(...)`. Each algorithm of every Pattern lands as a classmethod on the Pattern class. The algorithm-config module loads lazily on first call. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* ✨ `grelmicro.resilience` is now a PEP 562 lazy package: `from grelmicro.resilience import CircuitBreaker` no longer loads `RateLimiter`, its algorithm configs, or memory/redis adapters. Same in the other direction. Top-level `__getattr__` dispatches to the right subpackage on first access. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).

### Breaking

* 💥 `CircuitBreaker.transition_to_closed`, `transition_to_open`, `transition_to_half_open`, `transition_to_forced_open`, `transition_to_forced_closed`, and `restart` are now `async def`. Add `await` at every call site. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* 💥 `CircuitBreakerBackend` Protocol is now lifecycle + `bind(name, config)`. Custom backends should return a `CircuitBreakerStrategy` instance from `bind`. `register(breaker)` and the local fast-path are dropped: every backend (including `MemoryCircuitBreakerAdapter`) goes through the Strategy. Memory state lives in adapter-owned dicts keyed by breaker name. `CircuitBreakerSharedState` renamed to `CircuitBreakerSnapshot`. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* 💥 `CircuitBreakerConfig` is now a `Discriminator("kind")`-tagged union (matches `RateLimiterConfig`). Instantiate `ConsecutiveCountConfig(...)` directly. The algorithm config lives at `grelmicro.resilience.circuitbreaker.consecutive_count`. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* 💥 `FORCED_OPEN` and `FORCED_CLOSED` no longer increment `consecutive_error_count` / `consecutive_success_count`. Per-replica `total_error_count` / `total_success_count` still tick. Dashboards keying off consecutive counts during forced states need updating. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* 💥 Resilience layout: each Pattern is now a subpackage with its algorithm configs and adapters as siblings. `grelmicro.resilience.memory` → `grelmicro.resilience.circuitbreaker.memory` and `grelmicro.resilience.ratelimiter.memory`. `grelmicro.resilience.redis` → `grelmicro.resilience.circuitbreaker.redis` and `grelmicro.resilience.ratelimiter.redis`. `grelmicro.resilience.algorithms` is gone: rate-limiter configs live at `grelmicro.resilience.ratelimiter.{token_bucket,sliding_window}`, circuit-breaker configs at `grelmicro.resilience.circuitbreaker.consecutive_count`. Top-level `from grelmicro.resilience import ...` shortcuts are unchanged. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* 💥 Rename Components: `Breaker` → `CircuitBreakers`, `RateLimit` → `RateLimiters`. Plural matches existing Component convention (`Tasks`, `HealthChecks`). Mechanical migration: replace `Breaker(...)` with `CircuitBreakers(...)` and `RateLimit(...)` with `RateLimiters(...)` at every call site. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* 💥 `CircuitBreaker.__init__` drops the algorithm kwargs path (`error_threshold=`, `success_threshold=`, `reset_timeout=`, `half_open_capacity=`, `log_level=`, `ignore_exceptions=`, `env_prefix=`, `env_load=`). Signature is now `CircuitBreaker(name, config=None, *, backend=None)`, matching `RateLimiter`. Use `CircuitBreaker.consecutive_count("name", error_threshold=5, ...)` for the simple case, `CircuitBreaker("name", ConsecutiveCountConfig(...))` for the declarative case, or bare `CircuitBreaker("name")` for defaults. Env loading via `GREL_CIRCUIT_BREAKER_*` is gone: build the config from `pydantic-settings` if you need that. Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).
* ✨ `CircuitBreaker` and `RateLimiter` fall back to a process-global implicit `MemoryCircuitBreakerAdapter` / `MemoryRateLimiterAdapter` when no `CircuitBreakers` / `RateLimiters` Component is registered. `CircuitBreaker("payments")` and `RateLimiter.token_bucket("api", capacity=10, refill_rate=1)` work without any `Grelmicro(uses=[...])` setup. Fleet-wide opt-in stays explicit (`Grelmicro(uses=[redis, CircuitBreakers(redis), RateLimiters(redis)])`). Issue [#163](https://github.com/grelinfo/grelmicro/issues/163).

## 0.23.0 - 2026-05-17

### Breaking

* 💥 Rename the discriminator field from `type` to `kind` on every tagged union. Affects `RateLimiterConfig` (`TokenBucketConfig`, `SlidingWindowConfig`) and `RetryBackoffConfig` (`ExponentialBackoff`, `ConstantBackoff`, `LinearBackoff`, `FibonacciBackoff`, `RandomBackoff`). Serialized YAML and JSON configs must replace `type:` with `kind:` (for example `GREL_RETRY_FOO_BACKOFF={"kind":"exponential",...}`). Frees the Python `type` builtin from being shadowed on every config object. Issue [#268](https://github.com/grelinfo/grelmicro/issues/268).
* 💥 Rename `GCRAConfig` to `SlidingWindowConfig` and `RateLimiter.gcra(...)` to `RateLimiter.sliding_window(...)`. The discriminator value also moves from `"gcra"` to `"sliding_window"`. Module `grelmicro.resilience.algorithms.gcra` becomes `grelmicro.resilience.algorithms.sliding_window`. Internal strategy classes (`_RedisGCRA`, `_MemoryGCRA`) keep their names since they describe the underlying algorithm. Issue [#259](https://github.com/grelinfo/grelmicro/issues/259).

### Features

* ✨ Add `Log` and `Trace` components. Register `Log()` and `Trace()` in `Grelmicro(uses=[...])` to wire observability through the same verb as `Sync`, `Cache`, `RateLimit`, `Breaker`, and `Tasks`. `Log()` wraps `grelmicro.log.configure(...)` and snapshots stdlib root handlers on enter so sequential apps in tests do not stack handlers. `Trace()` owns an OTel `TracerProvider`: builds it from `TracingConfig` (env prefix `GREL_TRACE_`), installs it on enter, shuts it down and restores the prior global provider on exit. OTLP HTTP and gRPC exporters are lazy-imported. Issue [#224](https://github.com/grelinfo/grelmicro/issues/224).

### Docs

* 📝 Add [Testing](architecture/testing.md) page documenting `micro.override(...)` and the conftest recipe. Issue [#236](https://github.com/grelinfo/grelmicro/issues/236).
* 📝 Add [Capability matrix](capabilities.md) page covering Pattern × Adapter pairs for `1.0.0`. Issue [#161](https://github.com/grelinfo/grelmicro/issues/161).

## 0.22.0 - 2026-05-16

### Features

* ✨ Add `Grelmicro` app object and `Component` protocol. The user composes everything attached to the app into one container and opens it with `async with micro:`. Single `Grelmicro.use(item)` registration verb (and `uses=` constructor kwarg) accepts `Component` instances (registered with `(kind, name)` lookup, exposed on `micro.<kind>`), first-party backends (auto-wrapped into their matching Component: `RedisCacheAdapter` → `Cache`, `RedisSyncAdapter` → `Sync`), and any other async context manager (lifecycled only, caller keeps the reference). Typed accessors `micro.sync` and `micro.cache` provide IDE completion. `Grelmicro.components` returns the registered Components in order for `/healthz`-style introspection. Issue [#208](https://github.com/grelinfo/grelmicro/issues/208), epic [#201](https://github.com/grelinfo/grelmicro/issues/201), unified in [#219](https://github.com/grelinfo/grelmicro/issues/219), `Component` rename and `.components` accessor in [#233](https://github.com/grelinfo/grelmicro/issues/233).
* ✨ Add `Sync` component. Wraps a `SyncBackend` and exposes `lock(...)`, `task_lock(...)`, `leader_election(...)` factories. Use it via `Grelmicro(uses=[redis, Sync(redis)])` (Provider-direct) or `Sync(MemorySyncAdapter())` (Backend-direct). Reach it on `micro.sync`. Issue [#210](https://github.com/grelinfo/grelmicro/issues/210).
* ✨ Add `Cache` component. Wraps a `CacheBackend` and exposes a `ttl(...)` factory that builds a `TTLCache` bound to the wrapped backend. Use it via `Grelmicro(uses=[redis, Cache(redis)])` (Provider-direct) or `Cache(MemoryCacheAdapter())` (Backend-direct). Reach it on `micro.cache`. Issue [#212](https://github.com/grelinfo/grelmicro/issues/212).
* ✨ Add Component-direct Provider API. `Sync`, `Cache`, `RateLimit`, and `Breaker` accept a `Provider` or a `Backend` instance. When given a Provider, the Component calls `provider.sync()`, `provider.cache()`, `provider.ratelimiter()`, or `provider.breaker()` to build the matching adapter. Add `Provider` base class in `grelmicro.providers._base` with the four factory methods. Add `RedisProvider.ratelimiter()` returning a `RedisRateLimiterAdapter`. The Adapter classes stay public as escape hatches for custom Providers, but the recommended user code uses `Sync(redis)` instead of `Sync(RedisSyncAdapter(provider=redis))`.
* ✨ Add `Grelmicro.current()` classmethod for ambient lookup. Inside `async with micro:` it returns the active app for the current asyncio task.
* ✨ Add `Retry` primitive with decorator, block, and class forms. Five backoff algorithms ship: `ExponentialBackoff` (default, with full jitter), `ConstantBackoff`, `LinearBackoff`, `FibonacciBackoff`, and `RandomBackoff`. `when=` is required and accepts a `Match` (or shorthand). Live reconfiguration via `Reconfigurable[RetryConfig]`. Three-paths configuration. Underlying exception is re-raised with a PEP 678 note on exhaustion. Issue [#165](https://github.com/grelinfo/grelmicro/issues/165).
* ✨ Add `Match` and `Outcome` to `grelmicro.resilience`. `Match` is the resilience-wide outcome filter DSL: `Match.exception(...)`, `Match.result(...)`, `Match.exception_message(...)`, `Match.exception_cause(...)`, `Match.predicate(...)`, `Match.always()`, `Match.never()` plus their `not_*` twins, composed with the `|` and `&` operators. `Outcome[T]` is the dataclass passed to custom predicates (`exception`, `result`, `raised`). Issue [#242](https://github.com/grelinfo/grelmicro/issues/242).
* ✨ Add `grelmicro.providers.redis.RedisProvider`. First-class Redis connection holder shared across components: `RedisProvider("redis://...")`, `RedisProvider(host="...", port=...)`, `RedisProvider()` (env-driven via `REDIS_*`), `RedisProvider.from_config(RedisConfig(...))`, and `RedisProvider.from_client(client, own=False)` for bring-your-own clients. `Grelmicro` dedupes implicit providers by `(provider_class, env_prefix)`, so two adapters with the same prefix share one connection pool. Issue [#226](https://github.com/grelinfo/grelmicro/issues/226).
* ✨ Add `grelmicro.providers.postgres.PostgresProvider`. First-class Postgres connection holder wrapping an `asyncpg.Pool`: `PostgresProvider("postgresql://...")`, `PostgresProvider(host=..., database=..., user=..., password=...)`, `PostgresProvider()` (env-driven via `POSTGRES_*`), `PostgresProvider.from_config(PostgresConfig(...))`, and `PostgresProvider.from_client(pool, own=False)` for bring-your-own pools. Shares the same `(provider_class, env_prefix)` dedupe as `RedisProvider`. Issue [#255](https://github.com/grelinfo/grelmicro/issues/255).

### Breaking

* 💥 Patterns `RateLimiter`, `CircuitBreaker`, and the FastAPI health router resolve through the active `Grelmicro` app. Add the two new Components `RateLimit` (wraps `RateLimiterBackend`, kind `"ratelimiter"`) and `Breaker` (wraps `CircuitBreakerBackend`, kind `"circuitbreaker"`). `Grelmicro.use(...)` auto-wraps a `RateLimiterBackend` or `CircuitBreakerBackend` instance into its matching Component. `HealthChecks` becomes a Component (`kind = "health"`, default `name = "default"`). Pass it to `Grelmicro(uses=[...])` and the FastAPI `health_router()` resolves it via `Grelmicro.current()`. Delete the `rate_limiter_backend_registry`, `circuit_breaker_backend_registry`, `health_checks` registries plus `grelmicro/_backends.py` (`BackendRegistry`, `BackendNotLoadedError`, `BackendAlreadyRegisteredError`). Closes out [#201](https://github.com/grelinfo/grelmicro/issues/201). Issue [#261](https://github.com/grelinfo/grelmicro/issues/261).
* 💥 Redis adapters now take `provider=` or `env_prefix=`, not a positional `url=`. `RedisSyncAdapter`, `RedisCacheAdapter`, and `RedisRateLimiterAdapter` lose their `url=` argument. Pass `provider=RedisProvider(...)` to share a pool, or rely on `env_prefix=` (default `REDIS_`) to build one. Issue [#226](https://github.com/grelinfo/grelmicro/issues/226).
* 💥 `PostgresSyncAdapter` now takes `provider=` or `env_prefix=`, not a positional `url=`. Pass `provider=PostgresProvider(...)` to share a pool, or rely on `env_prefix=` (default `POSTGRES_`) to build one. Issue [#255](https://github.com/grelinfo/grelmicro/issues/255).
* 💥 Rename `TaskManager` to `Tasks`. Class still extends `TaskRouter`, mirroring FastAPI's `APIRouter` ← `FastAPI` shape. Update imports to `from grelmicro.task import Tasks`. Issue [#218](https://github.com/grelinfo/grelmicro/issues/218).
* 💥 Rename `HealthRegistry` to `HealthChecks` (and `HealthRegistryConfig` to `HealthChecksConfig`). Update imports to `from grelmicro.health import HealthChecks`. Issue [#201](https://github.com/grelinfo/grelmicro/issues/201).
* 💥 Rename concrete backends to `*Adapter`. `MemorySyncBackend`, `RedisSyncBackend`, `PostgresSyncBackend`, `SQLiteSyncBackend`, `KubernetesSyncBackend`, `MemoryCacheBackend`, `RedisCacheBackend`, `MemoryRateLimiterBackend`, `RedisRateLimiterBackend`, and `MemoryCircuitBreakerBackend` become `*Adapter`. The `SyncBackend`, `CacheBackend`, `RateLimiterBackend`, and `CircuitBreakerBackend` Protocols stay as-is. Issue [#201](https://github.com/grelinfo/grelmicro/issues/201).
* 💥 Rename nested backoff classes to drop the redundant `Config` suffix. `ExponentialBackoffConfig`, `ConstantBackoffConfig`, `LinearBackoffConfig`, `FibonacciBackoffConfig`, and `RandomBackoffConfig` become `ExponentialBackoff`, `ConstantBackoff`, `LinearBackoff`, `FibonacciBackoff`, and `RandomBackoff`. The `RetryBackoffConfig` discriminated-union alias and the JSON discriminator (`type: "exponential"`) are unchanged. Issue [#239](https://github.com/grelinfo/grelmicro/issues/239).
* 💥 Rename the `Module` protocol to `Component` to avoid clashing with Python's own "module". `ModuleAlreadyRegisteredError` becomes `ComponentAlreadyRegisteredError` and `ModuleNotRegisteredError` becomes `ComponentNotRegisteredError`. The `Sync` and `Cache` classes keep their names. No deprecation shim. Issue [#233](https://github.com/grelinfo/grelmicro/issues/233).
* 💥 Rename the env-loading flag to align the per-call kwarg and the global env var. `read_env=` becomes `env_load=` on every component, `GREL_CONFIG_FROM_ENV` becomes `GREL_ENV_LOAD`, and the `grelmicro._config.env_opt_in_enabled()` helper becomes `env_load_default()`. No deprecation shim. Issue [#232](https://github.com/grelinfo/grelmicro/issues/232).
* 💥 Remove the module-level registry and lifespan API. `grelmicro.lifespan()` is gone. The `register` / `unregister` / `use` / `use_backend` / `use_registry` helpers across `grelmicro.{sync,cache,health,resilience}` (plus the resilience circuit-breaker variants) are gone. Patterns (`Lock`, `TaskLock`, `LeaderElection`, `TTLCache`) now resolve their backend via `Grelmicro.current()` at every call. Build a `Grelmicro(uses=[...])` and open it with `async with micro:`. The `grelmicro.sync._backends` and `grelmicro.cache._backends` modules are removed (sync and cache resolve through the app). The internal `rate_limiter_backend_registry`, `circuit_breaker_backend_registry`, and `health_checks` registries stay private until follow-up issues introduce their Component wrappers. Issue [#207](https://github.com/grelinfo/grelmicro/issues/207).
* 💥 `Retry` outcome filter is now `when=` accepting a `Match`. The old `on=` parameter is gone. The `Match` DSL (`Match.exception(...)`, `Match.result(...)`, `Match.exception_message(...)`, `Match.exception_cause(...)`, `Match.always()`, `Match.never()`, `Match.predicate(...)`) plus their `not_*` twins and the `|`/`&` operators cover the common retry-filter surface. Bare-class shorthand is still accepted (`when=httpx.HTTPError`). Result-based retry lands in the same change: `when=Match.result(None)` retries until the function stops returning `None`. Env var renamed `GREL_RETRY_{NAME}_ON` → `GREL_RETRY_{NAME}_WHEN`. Issue [#242](https://github.com/grelinfo/grelmicro/issues/242).

### Internal

* ⚡ Defer the `opentelemetry` import in `grelmicro.trace`. `import grelmicro.trace` no longer loads `opentelemetry` (was 16 modules). The package is resolved lazily on first call to `instrument`, `span`, or `add_context` and cached. Issue [#189](https://github.com/grelinfo/grelmicro/issues/189).

## 0.21.0 - 2026-05-06

### Breaking

* 💥 Drop Python 3.11. The new floor is `requires-python = ">=3.12"`. RHEL 9 (App Stream `python3.12`) and RHEL 10 (default) ship 3.12 and the UBI images are available, so enterprise users are covered. Issue [#66](https://github.com/grelinfo/grelmicro/issues/66).
* 💥 Drop AnyIO. grelmicro now targets `asyncio` directly. Issue [#183](https://github.com/grelinfo/grelmicro/issues/183).
* 💥 `CircuitBreaker` now takes a backend (``CircuitBreakerBackend``). The in-memory backend (``MemoryCircuitBreakerBackend``) is the default. A future Redis-backed implementation will share state across replicas (issue #188). The async API stays primary, sync code goes through ``cb.from_thread``.
* 💥 The sync adapters on `Lock`, `TaskLock`, `TTLCache`, and `CircuitBreaker` now require the backend to be opened (``async with backend:`` or ``grelmicro.lifespan()``). The backend captures the running loop and the sync adapter dispatches through it. Zero hot-path overhead.
* 💥 Resilience registries are now namespaced. The rate limiter registry name moves from ``"resilience"`` to ``"resilience.ratelimiter"`` and the circuit breaker registry is ``"resilience.circuitbreaker"``. ``grelmicro.lifespan(exclude=...)`` now matches by dotted prefix, so ``exclude={"resilience"}`` still skips both registries.

### Features

* ✨ Add `uvloop` to the `standard` extra (Linux and macOS). Activate with `uvloop.run(main())`.

### Internal

* ✅ Migrate the test suite from `pytest.mark.anyio` to `pytest-asyncio` with `asyncio_mode = "auto"`. AnyIO is no longer a direct dependency of grelmicro (it may still arrive transitively, for example through `fast-depends`).
* ♻️ Adopt PEP 695 generic syntax (`class Foo[T]:`, `def f[T](...)`, `type X = ...`) across `_backends.py`, `_config.py`, `_types.py`, `health/_types.py`, `trace/_instrument.py`, and `tests/task/conftest.py`. Two files keep the older form: the recursive aliases in `_json.py` (ty cannot expand recursive PEP 695 aliases) and the decorator factory in `cache/cached.py` (PEP 695 binds the inner decorator to the outer scope's type parameters, breaking per-decoration-site inference). Issue [#65](https://github.com/grelinfo/grelmicro/issues/65).
* 🔨 Bump `tool.ruff.target-version` to `py312` and the CI matrix to `["3.12","3.13","3.14"]`.

## 0.20.0 - 2026-05-03

Live reconfiguration is complete. Every stateful primitive now exposes `reconfigure(new_config)`, so you can hot-reload from a `ConfigMap` or SIGHUP without restarting the process. See [Live reconfiguration](architecture/reconfigure.md) for the contract.

### Features

* ✨ Add `RateLimiter.reconfigure(new_config)`. Swap algorithm config without rebuilding the limiter. PR [#153](https://github.com/grelinfo/grelmicro/pull/153).
* ✨ Add `reconfigure(new_config)` to `Lock`, `TaskLock`, and `LeaderElection`. Swap timing fields without restarting. The `worker` field cannot change. PR [#159](https://github.com/grelinfo/grelmicro/pull/159).
* ✨ Add `CircuitBreaker.reconfigure(new_config)`. Swap thresholds and `ignore_exceptions` without restarting. Runtime state and `last_error` are kept. `log_level` is applied to the logger. PR [#160](https://github.com/grelinfo/grelmicro/pull/160).
* ✨ Add `HealthRegistry.reconfigure(new_config)`. Swap `cache_ttl` and the default `timeout` without restarting. Per-check timeouts stay as registered. PR [#180](https://github.com/grelinfo/grelmicro/pull/180).

### Docs

* 📝 Reframe README and docs landing as a microservice patterns toolkit. PR [#155](https://github.com/grelinfo/grelmicro/pull/155).
* 📝 Replace "Production-ready" with "Railguarded": 100% pytest coverage, ty-checked, ruff-linted, Pydantic-validated. PR [#181](https://github.com/grelinfo/grelmicro/pull/181).

### Internal

* 🔨 Switch build backend to Hatch. PR [#155](https://github.com/grelinfo/grelmicro/pull/155).
* 🎨 Supersample favicon PNGs with Lanczos downscaling for smoother anti-aliasing. PR [#156](https://github.com/grelinfo/grelmicro/pull/156).

## 0.19.0 - 2026-05-01

Cleans out the long-deprecated APIs (`ResilienceException`, `Synchronization`, `scheduled()`, the `token=` kwarg) ahead of the 1.0.0 design work, ships a 3.4× speedup on env-driven config construction, and brings the test suite under 20s for contributors.

### Breaking

* 💥 The Environmental config path is now opt-in. Set `GREL_CONFIG_FROM_ENV=true` once at startup to enable env reads across every component, or pass `read_env=True` per call. The per-call value (`True`/`False`) always wins over the global flag. This stops grelmicro from silently picking up ambient env vars in unit tests or scripts. Issue [#142](https://github.com/grelinfo/grelmicro/issues/142).
* 💥 The `read_env` kwarg default flips from `True` to `None` on every component. `None` follows the global flag. `True` and `False` keep their meaning as explicit per-call overrides.
* 💥 Remove obsolete deprecation shims that were marked for removal in 0.7.0. Replace `ResilienceException` with `ResilienceError`, `Synchronization` with `SyncPrimitive`, and the `scheduled()` decorator on `TaskRouter` / `TaskManager` with `interval(seconds=N, max_lock_seconds=N*5)`. The `token=` kwarg on `LockAcquireError`, `LockReleaseError`, and `LockNotOwnedError` is removed (drop it from your code). The `sync=` parameter on `interval()` no longer warns when used with non-`Lock` primitives.

### Internal

* ♻️ Add `grelmicro._config.env_opt_in_enabled()` helper that exposes the truthy `GREL_CONFIG_FROM_ENV` check (`1`, `true`, `yes`, `on`, case-insensitive). Issue [#142](https://github.com/grelinfo/grelmicro/issues/142).
* 📝 Document the "no field-mirroring" decision in `docs/architecture/config.md` with the benchmark numbers from `benchmarks/config_attr_benchmark.py`. Closes Issue [#113](https://github.com/grelinfo/grelmicro/issues/113) without code changes: hot-path config reads cost <1% of a real call (~2 ns out of ~250 ns), so we keep `self._config` as the single source of truth instead of copying frozen fields onto the component.
* 🔇 Silence the upstream `testcontainers` `@wait_container_is_ready` deprecation banner via a scoped `filterwarnings` entry in `pyproject.toml`. Replace the unawaited `lambda: sleep(math.inf)` mock side-effect in `tests/sync/test_leaderelection.py::test_leadership_abandon_on_renew_deadline_reached` with an explicit async helper. The full suite now reports zero warnings.
* ⚡ Speed up the test suite from ~73s to ~19s by adding `pytest-xdist` (`-n auto` in `addopts`) and shrinking expiration sleeps in `tests/sync/test_backends.py`. Fix `--durations` reporting by removing the autouse `freeze_time` fixture: `@freeze_time()` decorator stays on the two tests that compare `datetime.now()`, the two tests that previously called `frozen_time.tick(...)` switch to `monkeypatch.setattr(circuitbreaker, "monotonic", ...)`. Issue [#125](https://github.com/grelinfo/grelmicro/issues/125).
* ⚡ Cache the dynamic `BaseSettings` subclass built by `grelmicro._config._build_settings_cls` with `@functools.lru_cache(maxsize=256)`. The env path of `resolve_config` now reuses the same `_<Config>Settings` subclass across calls instead of rebuilding it every time, which makes `Lock("cart")`-style construction ~3.4× faster (232 µs/op → 68 µs/op). The bound is a safety net for long-running processes that might derive prefixes from runtime inputs. Issue [#119](https://github.com/grelinfo/grelmicro/issues/119).
* ♻️ Rename the local `parent_config` to `merged_config` in `_build_settings_cls` and document why the existing `# type: ignore` comments are needed. Issue [#127](https://github.com/grelinfo/grelmicro/issues/127).

## 0.18.0 - 2026-04-30

M2 milestone closed: backend wiring is now fully explicit. Construction is pure (no global writes), registration is named (`<module>.register(backend, "name")`), and `grelmicro.lifespan(*ad_hoc, exclude=...)` walks every registry that has been imported and opens its registered backends in one call. Task-scoped overrides via `with <module>.use(...):` swap backends per request or per test through `contextvars`.

### Breaking

* 💥 Backend constructors are now pure: `__init__` performs no registry writes. The `auto_register` kwarg is removed from every backend and from `HealthRegistry`. PR [#138](https://github.com/grelinfo/grelmicro/pull/138).
* 💥 `BackendRegistry.set` is renamed `register` and `BackendRegistry.unregister` is added with an identity check. `reset` remains for test fixtures. PR [#138](https://github.com/grelinfo/grelmicro/pull/138).
* 💥 `async with backend` opens the connection but no longer registers. Call `register(backend)` (or `use_backend(backend)`) to register, or open everything at once with `grelmicro.lifespan()`. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* 💥 `BackendRegistry` is now multi-name: `register(backend, name="default")`, `unregister(name, backend=None)`, `get(name="default")`. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* 💥 The sync registry name changed from `"lock"` to `"sync"` (used in error messages and `lifespan()` exclude keys). The rate limiter registry changed from `"rate_limiter"` to `"resilience"`. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* 💥 Overwriting a registered name with a different instance now raises `BackendAlreadyRegisteredError` (was: warning + replace). Re-registering the same instance stays a no-op. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).

### Features

* ✨ Add `grelmicro.sync.use_backend`, `grelmicro.cache.use_backend`, `grelmicro.resilience.use_backend`, and `grelmicro.health.use_registry` for explicit, idempotent process-lifetime registration. PR [#138](https://github.com/grelinfo/grelmicro/pull/138).
* ✨ `grelmicro.lifespan(*ad_hoc, exclude=...)` opens every registered backend across every imported module in one call, with reverse-order shutdown. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* ✨ Per-module helpers `register`, `unregister`, `use_backend`, `use` on `grelmicro.sync`, `grelmicro.cache`, `grelmicro.resilience` (and `use_registry`, `use` on `grelmicro.health`). PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* ✨ Task-scoped overrides via `<module>.use(backend)` or `<module>.use(default=a, analytics=b)`. Stacks LIFO via `contextvars` for per-test and per-request substitution. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* ✨ Primitives accept `backend=` as either a backend instance or a registered name (`Lock("audit", backend="analytics")`). The registry is consulted on each call so `<module>.use(...)` overrides apply. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* ✨ Registries subscribe themselves on import: `lifespan()` walks only modules that are actually imported, so unused components have zero RAM cost and zero startup work. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).
* ✨ Lookup falls back to the sole registered entry when no `"default"` is named, so the single-backend case stays one-call. PR [#139](https://github.com/grelinfo/grelmicro/pull/139).

## 0.17.0 - 2026-04-29

### Breaking

* 💥 `CircuitBreaker` config moves to a frozen `CircuitBreakerConfig`. Read it via `cb.config`. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* 💥 The mutable attributes `cb.error_threshold`, `cb.success_threshold`, `cb.reset_timeout`, `cb.half_open_capacity`, `cb.ignore_exceptions`, `cb.log_level` are removed. Construct a new `CircuitBreaker` to change config. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* 💥 Rename `grelmicro.logging` to `grelmicro.log` and `grelmicro.tracing` to `grelmicro.trace`. Avoids shadowing stdlib `logging` and aligns with the OpenTelemetry / `ddtrace` `trace` (singular) convention. Update imports: `from grelmicro import log, trace`. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* 💥 `configure_logging()` is renamed `log.configure()`. Use `log.configure_with(config)` for the declarative path. Both return the applied `LoggingConfig`. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* 💥 `LoggingSettings` (the `BaseSettings` shadow class) is removed. `LoggingConfig` is the config class. Env reading happens inside `log.configure()`. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* 💥 `LoggingConfig` field names move to lowercase: `LOG_BACKEND` → `backend`, `LOG_LEVEL` → `level`, `LOG_FORMAT` → `format`, `LOG_TIMEZONE` → `timezone`, `LOG_JSON_SERIALIZER` → `json_serializer`, `LOG_CALLER_ENABLED` → `caller_enabled`, `LOG_OTEL_ENABLED` → `otel_enabled`. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* 💥 Env vars move from `LOG_*` to `GREL_LOG_*` to align with the rest of the library. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* 💥 `LoggingSettingsValidationError` is removed. `pydantic.ValidationError` propagates from `log.configure()` like every other component. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).

### Features

* ✨ Add `CircuitBreakerConfig` and `CircuitBreaker.from_config(name, config)`. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* ✨ `CircuitBreaker` reads `GREL_CIRCUIT_BREAKER_<NAME>_*` env vars and accepts `env_prefix=` / `read_env=`. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* ✨ `ignore_exceptions` accepts fully-qualified import strings (`"builtins.ValueError"`) so YAML and env loaders can specify exception types. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* ✨ Env vars for tuple/list fields accept comma-separated values in addition to JSON arrays. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* ✨ `log.configure(**kwargs)` accepts every `LoggingConfig` field as a kwarg, mirroring the three-paths contract used by other components. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).
* ✨ `log.configure_with(config)` is the declarative entry point. Returns the applied `LoggingConfig`. PR [#135](https://github.com/grelinfo/grelmicro/pull/135).

### Internal

* ♻️ Add `grelmicro/_types.py` for shared lightweight type aliases (`LogLevel`). PR [#132](https://github.com/grelinfo/grelmicro/pull/132).
* ♻️ Add `grelmicro/_config.py::parse_csv_or_json` shared utility for env var list parsing. PR [#132](https://github.com/grelinfo/grelmicro/pull/132).

### Docs

* 🎨 Switch logo typeface from Funnel Sans to Funnel Display.

## 0.16.1 - 2026-04-29

### Internal

* ✅ "No registry call at construction" tests now patch the registry source instead of the per-module import alias, so a future refactor that bypasses the local alias can no longer silently pass the check. PR [#130](https://github.com/grelinfo/grelmicro/pull/130).
* ⬆️ Bump `ty` from 0.0.29 to 0.0.30. PR [#111](https://github.com/grelinfo/grelmicro/pull/111).
* ⬆️ Pre-commit autoupdate. PR [#114](https://github.com/grelinfo/grelmicro/pull/114).

## 0.16.0 - 2026-04-29

### Breaking

* 💥 `LockConfig`, `TaskLockConfig`, `LeaderElectionConfig`, and `RateLimiterConfig` no longer carry a `name` field. Pass the name positionally: `Lock("cart", LockConfig(lease_duration=30))`. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* 💥 Rename `TokenBucket` to `TokenBucketConfig` and `GCRA` to `GCRAConfig`. `RateLimiterConfig` becomes the discriminated union of algorithm configs. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* 💥 `RateLimiter` takes the algorithm config positionally: `RateLimiter("api", GCRAConfig(limit=100, window=60))`. The `algorithm=`, `limit=`, `window=` kwargs are removed. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* 💥 `fail_open` moves from `RateLimiter(...)` to the algorithm config. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).

### Features

* ✨ Add `Component.from_config(name, config)` to every primitive (`Lock`, `TaskLock`, `LeaderElection`, `RateLimiter`, `HealthRegistry`, `RateLimitFilter`, `DuplicateFilter`). PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* ✨ Read environment variables under `GREL_<COMPONENT>_<NAME>_*` for every component that supports the environmental path. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* ✨ Add `RateLimiter.token_bucket(name, ...)` and `RateLimiter.gcra(name, ...)` factory classmethods. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* ✨ Add `env_prefix=` and `read_env=` kwargs to every component that exposes the environmental path. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).
* ✨ Normalise instance names like `payments-eu`, `cart.v2`, or `weather/svc` into POSIX env var segments. PR [#123](https://github.com/grelinfo/grelmicro/pull/123).

### Changed

* ♻️ `Lock`, `TaskLock`, `LeaderElection`, and `RateLimiter` now resolve the backend lazily on first use instead of at construction. `BackendNotLoadedError` surfaces on the first `acquire`/`peek`/`reset` call rather than in `__init__`. Each component exposes a public `backend` property. PR [#128](https://github.com/grelinfo/grelmicro/pull/128).

### Fixed

* 🐛 Auto-registered backends now identity-check before clearing the registry on `__aexit__`, so a replacement instance is left alone. PR [#122](https://github.com/grelinfo/grelmicro/pull/122).
* 🐛 `Lock.release` clears local ownership only after the backend confirms the release. PR [#122](https://github.com/grelinfo/grelmicro/pull/122).

## 0.15.0 - 2026-04-29

### Breaking

* 💥 Redesign the `health` module: `@health.check("name")` decorator, binary `ok`/`error` status, empty probe bodies, per-check caching. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).
* 💥 Endpoint renames: `/health/live` → `/livez`, `/health/ready` → `/readyz`. New `/healthz` returns the full check JSON. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).
* 💥 `HealthRegistry.check()` renamed to `run()`. The `check` name is now the decorator. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).
* 💥 `HealthChecker` Protocol removed. Use plain `def` or `async def` functions. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).
* 💥 `HealthReport.components: list` becomes `HealthReport.checks: dict[name, ...]`. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).
* 💥 `HealthCheckTimeoutError` and the three-state `HealthStatus` removed. PR [#112](https://github.com/grelinfo/grelmicro/pull/112).

### Docs

* 📝 Restate the versioning policy: pre-1.0 `MINOR` may break, `PATCH` never. Post-1.0 deprecations get two `MINOR` releases.

## 0.14.3 - 2026-04-22

### Docs

* 🐛 Fix the wordmark duplicating on PyPI and other renderers that don't understand GitHub's theme-only URL fragments. PR [#109](https://github.com/grelinfo/grelmicro/pull/109).

## 0.14.2 - 2026-04-22

### Docs

* 🐛 Fix the landing-page wordmark disappearing when the docs site is toggled into dark mode. PR [#108](https://github.com/grelinfo/grelmicro/pull/108).
* 📝 Centre the badges row under the tagline. PR [#108](https://github.com/grelinfo/grelmicro/pull/108).

## 0.14.1 - 2026-04-22

### Docs

* 🎨 Ship the grelmicro brand identity: wordmark, favicon, and social-preview card. PR [#106](https://github.com/grelinfo/grelmicro/pull/106).
* 🎨 Refresh the docs theme with the brand palette. PR [#106](https://github.com/grelinfo/grelmicro/pull/106).
* 📝 Rewrite the "Why grelmicro" pillars. PR [#106](https://github.com/grelinfo/grelmicro/pull/106).
* 📝 Split the resilience docs into per-pattern pages.
* 📝 Add an Installation guide with `pip`, `uv`, and `poetry` tabs.
* 📝 Render PEP 727 `Annotated[..., Doc(...)]` parameter docs via `griffe-typingdoc`.
* 📝 Plain-English pass on docs and docstrings for non-native readers.
* 📝 Add a Mermaid state diagram to the Circuit Breaker page.
* 📝 Document every `__all__` symbol in the API reference.
* 📝 Add a plain-English style guide to `CONTRIBUTING.md`.

### Internal

* 🐛 De-flake `test_lock_reentrant_from_thread` on Python 3.12. Fixes [#105](https://github.com/grelinfo/grelmicro/issues/105).
* 🔧 Add keywords to `pyproject.toml` for PyPI discovery.

## 0.14.0 - 2026-04-21

### Features

* ✨ Add pluggable `RateLimiter` algorithms via the `algorithm=` parameter: `TokenBucket` and `GCRA`. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).
* ✨ Add `MemoryTokenBucket`, a standalone synchronous token-bucket primitive. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).
* ✨ Add `RateLimitFilter`, a `logging.Filter` with configurable `key_mode`. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).
* ✨ Add `DuplicateFilter`, a `logging.Filter` that caps repeated records per key with optional TTL. PR [#94](https://github.com/grelinfo/grelmicro/pull/94).
* ✨ `HealthRegistry` now logs every unhealthy path at `WARNING` (`ERROR` for unexpected exceptions). PR [#92](https://github.com/grelinfo/grelmicro/pull/92).

### Deprecations

* 🗑️ `RateLimiter(name, limit=..., window=...)` is deprecated. Use `RateLimiter(name, algorithm=GCRA(limit=..., window=...))` instead. Will be removed in 0.15.0. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).

### Docs

* 📝 Add [`CONTRIBUTING.md`](https://github.com/grelinfo/grelmicro/blob/main/CONTRIBUTING.md) with repo conventions. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).
* 📝 Add a "Choosing an algorithm" guide for `TokenBucket` vs `GCRA` in the [Rate Limiter](resilience/rate-limiter.md) docs. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).
* 📝 Surface `THIRD_PARTY_NOTICES.md` in the docs site. PR [#102](https://github.com/grelinfo/grelmicro/pull/102).

### Security

* 🔒️ Harden CI supply chain: pin all Actions to SHAs, close `run:` injection vectors, add zizmor workflow-lint, restrict Dependabot auto-merge to uv patch/minor updates. PRs [#95](https://github.com/grelinfo/grelmicro/pull/95), [#100](https://github.com/grelinfo/grelmicro/pull/100), [#101](https://github.com/grelinfo/grelmicro/pull/101).

### Internal

* ⬆️ Bump `pydantic` to 2.13.0, `opentelemetry-api` / `opentelemetry-sdk` to 1.41.0, `pytest` to 9.0.3, `ruff` to 0.15.10, `ty` to 0.0.29, `fastapi` to 0.135.3, `uvicorn` to 0.44.0. PR [#99](https://github.com/grelinfo/grelmicro/pull/99).
* ⬆️ Bump `pydantic-extra-types` from 2.11.1 to 2.11.2. PR [#89](https://github.com/grelinfo/grelmicro/pull/89).
* ⬆️ Pre-commit `ruff` autoupdate (v0.15.9 → v0.15.11). PR [#91](https://github.com/grelinfo/grelmicro/pull/91).
* ⬆️ Bump `codecov/codecov-action` to v6. PR [#96](https://github.com/grelinfo/grelmicro/pull/96).
* ⬆️ Bump `astral-sh/setup-uv` to v8. PR [#97](https://github.com/grelinfo/grelmicro/pull/97).
* ⬆️ Bump `dependabot/fetch-metadata` to v3. PR [#98](https://github.com/grelinfo/grelmicro/pull/98).

## 0.13.0 - 2026-04-08

### Features

* ✨ Add `RateLimiter.peek(key)`: check rate limit state without consuming tokens. PR [#90](https://github.com/grelinfo/grelmicro/pull/90).
* ✨ Add `RateLimiter.reset(key)`: delete rate limit state for a key, restoring full quota. PR [#90](https://github.com/grelinfo/grelmicro/pull/90).
* ✨ Add `fail_open` parameter to `RateLimiter`: return allowed result on backend errors instead of propagating exceptions. PR [#90](https://github.com/grelinfo/grelmicro/pull/90).

## 0.12.0 - 2026-04-07

### Features

* ✨ Add `health` module with health check registry, concurrent checker execution, and FastAPI integration for liveness/readiness probes. PR [#84](https://github.com/grelinfo/grelmicro/pull/84).

### Internal

* ⬆️ Bump orjson from 3.11.7 to 3.11.8. PR [#72](https://github.com/grelinfo/grelmicro/pull/72).
* ⬆️ Bump ty from 0.0.26 to 0.0.27. PR [#74](https://github.com/grelinfo/grelmicro/pull/74).
* ⬆️ Update uv-build requirement from `<0.10.0` to `<0.12.0`. PR [#75](https://github.com/grelinfo/grelmicro/pull/75).
* 👷 Add build provenance attestations and wheel verification to release pipeline.
* ♻️ Pre-release cleanup: add health/json to overview, fix style inconsistencies, remove stale branches.

## 0.11.0 - 2026-04-03

### Breaking Changes

* 💥 **Logging**: split `caller` into separate `logger` (logger name) and `caller` (`function:line`) fields. `caller` is now opt-in via `GREL_LOG_CALLER_ENABLED` (default: `False`), following common structured-logging conventions. Uvicorn formatter never includes `caller`.
* 💥 **Cache**: replace `TTLCache` `serializer`/`deserializer` callable pair with a single `serializer` accepting a `CacheSerializer` protocol object. Use `JsonSerializer()`, `PydanticSerializer(Model)`, or `PickleSerializer()` instead.

### Features

* ✨ Add `GREL_LOG_CALLER_ENABLED` setting to opt in to caller info (`function:line`) in log records. Disabled by default for cleaner logs and better performance.
* ✨ Add `logger` field (logger name, e.g., `myapp.api`) to all log records across all backends and formats.
* ✨ Add `grelmicro.json` module with fast JSON serialization using `orjson` when available, with automatic fallback to stdlib `json`.

## 0.10.0 - 2026-04-02

### Features

* ✨ Add `RateLimiter` to the `resilience` module: Redis-backed sliding-window rate limiting using the GCRA algorithm. Includes `RateLimitResult` with fields mapping to IETF rate limit headers, weighted requests via `cost` parameter, and `RateLimitExceededError`.

### Removals

* 🗑️ Remove deprecated `UvicornJSONFormatter` and `UvicornAccessJSONFormatter`. Use `UvicornFormatter` and `UvicornAccessFormatter` instead (deprecated since 0.9.1).

### CI

* ⚡ Migrate PyPI publishing from API token to OIDC trusted publishing.

## 0.9.1 - 2026-04-01

### Deprecations

* 🗑️ **`UvicornJSONFormatter` and `UvicornAccessJSONFormatter` are deprecated.** Use `UvicornFormatter` and `UvicornAccessFormatter` instead. The new formatters respect `GREL_LOG_FORMAT` instead of always producing JSON. Old names kept as aliases with `DeprecationWarning`.

## 0.9.0 - 2026-04-01

### Breaking Changes

* 💥 **`GREL_LOG_FORMAT` default changed from `JSON` to `AUTO`.** In production (non-TTY), behavior is identical (JSON output). In local dev (TTY), output switches to human-readable `TEXT` with colors. Set `GREL_LOG_FORMAT=JSON` explicitly to restore the previous default.

### Features

* ✨ Add `AUTO` log format (new default): detects TTY and selects `TEXT` (terminal) or `JSON` (piped/CI).
* ✨ Add `LOGFMT` log format: key-value pairs following the [logfmt](https://brandur.org/logfmt) convention, 30-40% smaller than JSON.
* ✨ Add `PRETTY` log format: multi-line indented output with structured error rendering.
* ✨ Enhanced `TEXT` format: now includes extra context fields as `key=value` pairs and supports ANSI colors.
* ✨ Add `NO_COLOR` / `FORCE_COLOR` environment variable support following [no-color.org](https://no-color.org) standard.

## 0.8.0 - 2026-04-01

### Breaking Changes

* 💥 **Backend imports moved to submodules.** Use `from grelmicro.sync.redis import RedisSyncBackend` instead of `from grelmicro.sync import RedisSyncBackend`. Same for all sync, cache, and logging backends. See [Import Strategy](architecture/imports.md).

### Features

* ✨ Add Uvicorn JSON formatters (`UvicornJSONFormatter`, `UvicornAccessJSONFormatter`) for structured logging via `dictConfig`.

## 0.7.0 - 2026-03-31

### Breaking Changes

* 💥 **Logging JSON format redesigned** to follow industry standards:
    * `logger` renamed to `caller`
    * `thread` removed
    * `ctx` removed: extra fields are now flat at the top level
    * `exception` replaced by structured `error` object (`type`, `message`, `stack`)

### Features

* ✨ Add `tracing` module with `@instrument` decorator, `span()` context manager, and `add_context()` for unified logging and OTel instrumentation.

### Performance

* ⚡ **Logging**: Up to +23% throughput across all backends.
* ⚡ Use `OrderedDict` for O(1) LRU operations in `TTLCache`.

### Refactors

* ♻️ Extract shared Redis config into `grelmicro/_redis.py`.
* ♻️ Make `TTLCache` generic and add `Doc` annotations.
* ♻️ Extract context stack into `grelmicro/_context.py` to decouple logging from tracing.
* ♻️ Filter private (`_`-prefixed) attributes from stdlib JSON log output.
* ♻️ Widen `@instrument(skip=...)` type from `set[str]` to `AbstractSet[str]`.

### Removals

* 🗑️ `Synchronization` protocol removed. Use `SyncPrimitive` instead (deprecated since 0.6.0).
* 🗑️ `ResilienceException` removed. Use `ResilienceError` instead (deprecated since 0.6.0).
* 🗑️ The `token` parameter on lock errors removed (deprecated since 0.6.0).
* 🗑️ The `sync` parameter on `interval()` removed (deprecated since 0.6.0).
* 🗑️ The `scheduled()` decorator removed (deprecated since 0.6.0).

## 0.6.0 - 2026-03-30

### Deprecations

* 🗑️ `Synchronization` protocol renamed to `SyncPrimitive`. The old name still works but emits a `DeprecationWarning`. Will be removed in 0.7.0.
* 🗑️ `ResilienceException` renamed to `ResilienceError`. The old name still works but emits a `DeprecationWarning`. Will be removed in 0.7.0.
* 🗑️ The `token` parameter on `LockAcquireError`, `LockReleaseError`, and `LockNotOwnedError` is deprecated. Tokens are no longer included in error messages for security. Will be removed in 0.7.0.
* 🗑️ The `sync` parameter on `interval()` for `TaskLock` and `LeaderElection` is deprecated. Use `max_lock_seconds` and `leader` parameters instead. Will be removed in 0.7.0.
* 🗑️ The `scheduled()` decorator is deprecated. Use `interval()` with `max_lock_seconds` or `leader` instead. Will be removed in 0.7.0.

### Features

* ✨ Add in-memory [TTL cache](cache/index.md) with LRU eviction, per-key stampede protection, and `@cached` decorator.
* ✨ Add `RedisCacheBackend` for distributed cache storage.
* ✨ Add cache statistics via `CacheInfo` (hits, misses, evictions, stampedes).
* ✨ Add Kubernetes sync backend using Lease resources (`pip install grelmicro[kubernetes]`).
* ✨ Add SQLite sync backend for home lab and local testing (`pip install grelmicro[sqlite]`).

### Security

* 🔒️ Remove token values from lock error messages to prevent leaking in logs.
* 🔒️ Upgrade `requests` to 2.33.0 (CVE fix in transitive dependency).

### Refactors

* ♻️ Unify error hierarchy under `GrelmicroError` base class. All module errors (`SyncError`, `ResilienceError`, `LoggingError`, `TaskError`, `CacheError`) now share a common base.
* ♻️ Use server-side timestamps and native Lease fields in sync backends.
* ♻️ Simplify token generation from UUID-based to string concatenation.
* ♻️ Harden TaskLock token nonce and error handling.

### Internal

* ✅ Achieve 100% library code coverage.
* 💚 Fix flaky integration test timeout in CI.
* ⬆️ Bump dependencies and fix ty v0.0.26 type errors.

### Docs

* 📝 Add [cache module](cache/index.md) documentation with usage guide and API reference.
* 📝 Add [Kubernetes Backend Architecture](architecture/kubernetes.md) page.
* 📝 Add [SQLite Backend Architecture](architecture/sqlite.md) page.
* 📝 Add backend comparison matrix to [Coordination](coordination/index.md#backends) guide.
* 📝 Rewrite README with project vision.

## 0.5.0 - 2026-03-17

### Breaking Changes

* 💥 Add namespace prefix to sync primitive backend keys (`lock:`, `tasklock:`, `leader:`). See [Migration Guide](#migration-guide) below.

### Features

* ✨ Add `TaskLock.from_thread` thread-safe adapter. PR [#57](https://github.com/grelinfo/grelmicro/pull/57).
* ✨ Add specific lock error classes (`LockAcquireError`, `LockReleaseError`, `LockLockedCheckError`, `LockOwnedCheckError`, `LockReentrantError`). PR [#57](https://github.com/grelinfo/grelmicro/pull/57).

### Refactors

* ♻️ Consolidate distributed lock and leader gating into the `interval()` decorator via `max_lock_seconds` and `leader` parameters. PR [#54](https://github.com/grelinfo/grelmicro/pull/54).

### Docs

* 📝 Add [Coordination Architecture](architecture/coordination.md) page. PR [#57](https://github.com/grelinfo/grelmicro/pull/57).

### Internal

* ⬆️ Bump redis, fastapi, pydantic, and pydantic-settings. PR [#55](https://github.com/grelinfo/grelmicro/pull/55).
* ⬆️ Update pre-commit hooks. PR [#50](https://github.com/grelinfo/grelmicro/pull/50).

### Migration Guide

#### Namespace-Prefixed Backend Keys

Prior versions used the `name` parameter directly as the backend key. Now each primitive adds a type-specific prefix:

| Primitive | Name | Backend Key |
|---|---|---|
| `Lock("my-resource")` | `my-resource` | `lock:my-resource` |
| `TaskLock("cleanup")` | `cleanup` | `tasklock:cleanup` |
| `LeaderElection("main")` | `main` | `leader:main` |

Existing locks stored in Redis or PostgreSQL will no longer match after upgrading. A running instance on the old version and one on the new version will **not** see each other's locks.

Upgrade all running instances together so they use the same key format. Old keys expire automatically via their lease duration (Redis `PEXPIRE` / PostgreSQL `expire_at`).

## 0.4.1 - 2026-03-13

### Docs

* 📝 Add Task Lock to synchronization primitives guide.

### Internal

* ⬆️ Bump actions/checkout to v6 and astral-sh/setup-uv to v7.

## 0.4.0 - 2026-03-13

### Features

* ✨ Add `TaskLock` for distributed task locking with auto-renewal.
* ✨ Add `GREL_LOG_TIMEZONE` support for configurable timezone in logging output.
* ✨ Add OpenTelemetry trace context injection into log records.
* ✨ Add `structlog` as alternative logging backend.
* ✨ Add configurable JSON serializer (`json` / `orjson`) for logging.

### Docs

* 📝 Add logging benchmark and performance documentation.

### Internal

* ⬆️ Bump orjson from 3.11.5 to 3.11.6. PR [#51](https://github.com/grelinfo/grelmicro/pull/51).
* ⬆️ Bump freezegun from 1.5.2 to 1.5.5. PR [#33](https://github.com/grelinfo/grelmicro/pull/33).

## 0.3.2 - 2026-01-27

### Internal

* 👷 Migrate from mypy to Astral ty for type checking. PR [#45](https://github.com/grelinfo/grelmicro/pull/45).
* 🔧 Add Python 3.14 support. PR [#47](https://github.com/grelinfo/grelmicro/pull/47).
* 🔧 Switch build system to `uv_build`. PR [#49](https://github.com/grelinfo/grelmicro/pull/49).
* 💚 Simplify CI and release workflow. PR [#24](https://github.com/grelinfo/grelmicro/pull/24).

## 0.3.1 - 2025-06-05

### Docs

* 📝 Add resilience patterns section and update links in README and index.

### Internal

* 💚 Fix release pipeline and GitHub Pages deployment permissions.

## 0.3.0 - 2025-06-05

### Features

* ✨ Add Circuit Breaker resilience pattern. PR [#18](https://github.com/grelinfo/grelmicro/pull/18).

### Docs

* 📝 Refactor code examples to use snippets.

### Internal

* 👷 Add Dependabot configuration for weekly updates.
* 🔒️ Fix workflow permission issues. PR [#21](https://github.com/grelinfo/grelmicro/pull/21), [#22](https://github.com/grelinfo/grelmicro/pull/22).

## 0.2.3 - 2024-12-04

### Features

* ✨ Add Redis key prefix support to avoid conflicts in shared instances.
* ✨ Add Redis and PostgreSQL settings management from environment variables.

## 0.2.2 - 2024-11-28

### Features

* ✨ Add PostgreSQL backend configuration from environment variables.

### Internal

* 🐛 Fix release workflow. PR [#7](https://github.com/grelinfo/grelmicro/pull/7), [#9](https://github.com/grelinfo/grelmicro/pull/9).

## 0.2.1 - 2024-11-26

### Internal

* 💚 Set up release workflow with version tagging.

## 0.2.0 - 2024-11-26

First public release.

### Features

* ✨ Add distributed `Lock` with lease-based expiration.
* ✨ Add `LeaderElection` for single-leader task execution.
* ✨ Add `IntervalTask` scheduler for periodic tasks with synchronization support.
* ✨ Add Redis, PostgreSQL, and in-memory synchronization backends.
* ✨ Add logging module with JSON and TEXT formatting via `GREL_LOG_LEVEL` and `GREL_LOG_FORMAT` environment variables.

### Docs

* 📝 Add MkDocs documentation site with Material theme.

### Internal

* 👷 Add unified CI workflow with linting, testing, and coverage.
