# Capability matrix

Find your feature and read its level. Levels are as of 2026-11-10. Nothing is Stable yet.

| Level | What it means | How it gets there |
| --- | --- | --- |
| 🚧 **Planned** | Not shipped yet. | It is on the [roadmap](roadmap.md). |
| 🧪 **Experimental** | New or unproven. The design can still change. Tell us what you find. | Every feature starts here. |
| 🔍 **Preview** | The design is decided. It still needs 3 quiet months. | CI runs it, it shipped at least 3 months ago, and no redesign is planned. |
| ✅ **Stable** | No break and no serious bug fix for 3 months, and no change announced. | A 🔍 feature stays quiet for 3 months. |
| ✅⚠️ **Stable, changing** | Stable, but it had a break or a serious bug fix in the last 3 months, or a change is announced. | 3 quiet months with nothing announced bring it back to ✅. |

A serious bug fix corrects a wrong result, lost or duplicated work, or a security hole. 🔍 goes back to 🧪 only when a redesign is announced.

A break or a serious bug fix counts for:

- The whole row, when it is on a Pattern.
- One cell, when it is on a backend.
- Every feature, when it affects them all, such as the Python floor or the `GREL_*` default.

From `1.0`, 🧪 is the only level the "nothing breaks before `2.0`" promise does not cover. A 🧪 feature can still break in a minor, and it always warns first.

A feature is only as mature as what it runs on. A level reports the past and promises nothing. Pin the minor whatever the level, as [Versions and upgrades](versioning.md#pin-the-minor) explains.

## Patterns on a backend

| Pattern | Memory | Redis | Redis Sentinel | Redis Cluster | Valkey | Postgres | SQLite | Kubernetes |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `Lock` | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> |
| `ReadWriteLock` | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> |
| `TaskLock` | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> |
| `LeaderElection` | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> |
| `@tasks.cron` | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Does not apply" aria-label="Does not apply">➖</span> |
| `Job` | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Planned" aria-label="Planned">🚧</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Does not apply" aria-label="Does not apply">➖</span> |
| `TTLCache` | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Does not apply" aria-label="Does not apply">➖</span> |
| `Idempotency` | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Does not apply" aria-label="Does not apply">➖</span> |
| `Outbox` | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Does not apply" aria-label="Does not apply">➖</span> | <span role="img" title="Does not apply" aria-label="Does not apply">➖</span> | <span role="img" title="Does not apply" aria-label="Does not apply">➖</span> | <span role="img" title="Does not apply" aria-label="Does not apply">➖</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Planned" aria-label="Planned">🚧</span> | <span role="img" title="Does not apply" aria-label="Does not apply">➖</span> |
| `RateLimiter` | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Does not apply" aria-label="Does not apply">➖</span> |
| `CircuitBreaker` | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Experimental" aria-label="Experimental">🧪</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Preview" aria-label="Preview">🔍</span> | <span role="img" title="Does not apply" aria-label="Does not apply">➖</span> |

➖ does not apply. Most 🧪 cells mean CI does not run it on that backend yet. There are three exceptions. `ReadWriteLock` is new, `Job` is new, and `LeaderElection` on SQLite is new. Valkey's Sentinel and Cluster modes are 🧪.

A level does not say whether replicas share state. See [the backend check](deployment.md#the-backend-check).

## Features with no backend

<table>
<thead><tr><th>Feature</th><th>Level</th></tr></thead>
<tbody>
<tr><th colspan="2">App</th></tr>
<tr><td><code>Grelmicro</code> setup and <code>install</code></td><td style="text-align:center"><span role="img" title="Preview" aria-label="Preview">🔍</span></td></tr>
<tr><td><code>ExternalConfig</code></td><td style="text-align:center"><span role="img" title="Preview" aria-label="Preview">🔍</span></td></tr>
<tr><td><code>@tasks.every</code></td><td style="text-align:center"><span role="img" title="Preview" aria-label="Preview">🔍</span></td></tr>
<tr><th colspan="2">Resilience</th></tr>
<tr><td><code>Retry</code></td><td style="text-align:center"><span role="img" title="Preview" aria-label="Preview">🔍</span></td></tr>
<tr><td><code>Bulkhead</code></td><td style="text-align:center"><span role="img" title="Preview" aria-label="Preview">🔍</span></td></tr>
<tr><td><code>Fallback</code></td><td style="text-align:center"><span role="img" title="Preview" aria-label="Preview">🔍</span></td></tr>
<tr><td><code>Timeout</code></td><td style="text-align:center"><span role="img" title="Preview" aria-label="Preview">🔍</span></td></tr>
<tr><td><code>Shield</code></td><td style="text-align:center"><span role="img" title="Preview" aria-label="Preview">🔍</span></td></tr>
<tr><td><code>Stack</code></td><td style="text-align:center"><span role="img" title="Experimental" aria-label="Experimental">🧪</span></td></tr>
<tr><th colspan="2">Observability</th></tr>
<tr><td><code>Log</code></td><td style="text-align:center"><span role="img" title="Preview" aria-label="Preview">🔍</span></td></tr>
<tr><td><code>Trace</code></td><td style="text-align:center"><span role="img" title="Preview" aria-label="Preview">🔍</span></td></tr>
<tr><td><code>Metrics</code></td><td style="text-align:center"><span role="img" title="Preview" aria-label="Preview">🔍</span></td></tr>
<tr><td><code>HealthChecks</code></td><td style="text-align:center"><span role="img" title="Preview" aria-label="Preview">🔍</span></td></tr>
<tr><td><code>AccessLog</code></td><td style="text-align:center"><span role="img" title="Experimental" aria-label="Experimental">🧪</span></td></tr>
<tr><td><code>OpsServer</code></td><td style="text-align:center"><span role="img" title="Experimental" aria-label="Experimental">🧪</span></td></tr>
<tr><th colspan="2">Security</th></tr>
<tr><td><code>ClientAddress</code></td><td style="text-align:center"><span role="img" title="Preview" aria-label="Preview">🔍</span></td></tr>
<tr><td><code>JWTVerifier</code></td><td style="text-align:center"><span role="img" title="Experimental" aria-label="Experimental">🧪</span></td></tr>
<tr><td><code>ClientBans</code></td><td style="text-align:center"><span role="img" title="Experimental" aria-label="Experimental">🧪</span></td></tr>
<tr><td><code>OAuthClient</code></td><td style="text-align:center"><span role="img" title="Experimental" aria-label="Experimental">🧪</span></td></tr>
<tr><th colspan="2">HTTP</th></tr>
<tr><td><code>AuthenticatedRequests</code></td><td style="text-align:center"><span role="img" title="Experimental" aria-label="Experimental">🧪</span></td></tr>
<tr><td><code>IdempotentRequests</code></td><td style="text-align:center"><span role="img" title="Preview" aria-label="Preview">🔍</span></td></tr>
<tr><td><code>CachedResponses</code></td><td style="text-align:center"><span role="img" title="Experimental" aria-label="Experimental">🧪</span></td></tr>
<tr><td><code>ConditionalRequests</code></td><td style="text-align:center"><span role="img" title="Experimental" aria-label="Experimental">🧪</span></td></tr>
<tr><td><code>RateLimitedRequests</code></td><td style="text-align:center"><span role="img" title="Experimental" aria-label="Experimental">🧪</span></td></tr>
<tr><td><code>ErrorResponses</code></td><td style="text-align:center"><span role="img" title="Experimental" aria-label="Experimental">🧪</span></td></tr>
<tr><th colspan="2">Frameworks</th></tr>
<tr><td>FastAPI</td><td style="text-align:center"><span role="img" title="Preview" aria-label="Preview">🔍</span></td></tr>
<tr><td>Starlette</td><td style="text-align:center"><span role="img" title="Preview" aria-label="Preview">🔍</span></td></tr>
<tr><td>FastStream</td><td style="text-align:center"><span role="img" title="Preview" aria-label="Preview">🔍</span></td></tr>
<tr><td>Litestar</td><td style="text-align:center"><span role="img" title="Experimental" aria-label="Experimental">🧪</span></td></tr>
</tbody>
</table>

🧪 means it shipped less than 3 months ago. A claimed `@tasks.every`, and a feature that stores data, such as `CachedResponses`, also depend on their Pattern on your backend. A rename does not restart the clock, so `IdempotentRequests` counts from `IdempotencyMiddleware`.

## Last changes

??? note "Last break and last serious bug fix, per feature"

    | Feature | Last break | Last serious fix | Announced |
    | --- | --- | --- | --- |
    | Whole library | 0.44.0 | None | |
    | `Lock` | 0.42.0 | 0.44.1 | |
    | `ReadWriteLock` | 0.42.0 | 0.44.2 | |
    | `TaskLock` | 0.42.0 | 0.42.5 | Gated tasks change in 0.45.0 |
    | `LeaderElection` | 0.42.0 | 0.42.0 | Gated tasks change in 0.45.0 |
    | `@tasks.cron` | 0.42.0 | 0.42.5 | Gated tasks change in 0.45.0 |
    | `Job` | None | None | |
    | `TTLCache` | 0.42.0 | 0.42.0 | |
    | `Idempotency` | 0.42.0 | 0.44.2 | |
    | `Outbox` | 0.42.0 | 0.42.5 | |
    | `RateLimiter` | 0.42.0 | 0.42.0 | |
    | `CircuitBreaker` | 0.42.0 | 0.42.0 | |
    | `Grelmicro` setup and `install` | 0.44.0 | 0.42.5 | A warning is removed in 0.45.0 |
    | `@tasks.every` | 0.42.0 | 0.42.5 | Gated tasks change in 0.45.0 |
    | `Retry` | 0.41.0 | 0.41.0 | |
    | `Bulkhead` | 0.42.0 | 0.41.0 | |
    | `Fallback` | 0.41.0 | 0.41.0 | |
    | `Timeout` | 0.41.0 | None | |
    | `Shield` | 0.42.0 | 0.40.0 | |
    | `Stack` | 0.41.0 | None | |
    | `Log` | 0.44.0 | 0.42.0 | Log format spelling changes in 0.45.0 |
    | `AccessLog` | 0.42.0 | 0.42.0 | |
    | `Trace` | 0.44.0 | 0.42.0 | |
    | `Metrics` | 0.44.0 | 0.41.0 | |
    | `HealthChecks` | 0.44.0 | 0.38.1 | A removal lands in 0.45.0 |
    | `OpsServer` | None | None | |
    | `ExternalConfig` | 0.40.0 | 0.40.0 | |
    | `ClientAddress` | 0.40.0 | 0.38.1 | |
    | `JWTVerifier` | 0.42.0 | 0.42.0 | An error class moves in 0.45.0 |
    | `ClientBans` | 0.42.0 | 0.42.0 | An error class moves in 0.45.0 |
    | `OAuthClient` | None | None | |
    | `AuthenticatedRequests` | None | None | |
    | `IdempotentRequests` | 0.42.0 | 0.44.2 | |
    | `CachedResponses` | 0.42.0 | 0.44.2 | |
    | `ConditionalRequests` | 0.42.0 | 0.42.0 | |
    | `RateLimitedRequests` | 0.42.0 | None | |
    | `ErrorResponses` | 0.42.0 | 0.41.0 | |
    | FastAPI | 0.42.0 | 0.42.0 | Imported names leave in 0.45.0 |
    | Starlette | 0.42.1 | 0.42.0 | Imported names leave in 0.45.0 |
    | Litestar | 0.42.0 | 0.44.2 | Imported names leave in 0.45.0 |
    | FastStream | 0.28.0 | 0.31.0 | |

    `0.44.0` reads `GREL_*` variables by default, which affects every feature. `0.42.6` fixed `database is locked` between SQLite providers, which affects every SQLite cell. `0.44.2` fixed URL redaction that could let a credential through, and it does not count for every feature.

    The [changelog](changelog.md) has the date of each version.

## Pick a backend

- **Memory**: tests and single-process apps.
- **Redis**: you run Redis and want the lowest latency. Change the URL to use Sentinel or Cluster, see [Providers](providers/index.md). On Cluster, each store keeps its keys in the right slots on its own.
- **Valkey**: you run Valkey.
- **Postgres**: Postgres is your only stateful service.
- **SQLite**: one host, and state must survive restarts. For several hosts, use Redis or Postgres.
- **Kubernetes**: `Lock`, `ReadWriteLock` or `LeaderElection` with no extra infrastructure.

To see how Patterns, backends and Providers fit together, read [Backends and Adapters](architecture/backends.md).
