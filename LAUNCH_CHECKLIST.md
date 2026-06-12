# grelmicro 1.0 launch checklist

Maintainer planning doc (not published to the docs site). Tracks the launch tasks that need a human: posting, badges, and the demo recording. Issues [#172](https://github.com/grelinfo/grelmicro/issues/172), [#177](https://github.com/grelinfo/grelmicro/issues/177), [#178](https://github.com/grelinfo/grelmicro/issues/178).

## Pre-launch (do before posting)

- [ ] Tag and publish the 1.0 release on PyPI; confirm `pip install grelmicro` resolves it.
- [ ] `docs/benchmarks.md` numbers re-run on a clean machine and dated.
- [ ] The [FastAPI demo](examples/fastapi-demo) starts from a fresh clone in three commands.
- [ ] Record the demo asset (see below) and embed it in the README.
- [ ] Enable the badges (see below).
- [ ] README "Why grelmicro" leads with one sentence a stranger understands.
- [ ] CHANGELOG `Unreleased` section moved under the `1.0` heading.

## Launch channels (#172)

Post in this order, spacing them out over a day so you can answer comments:

| Channel | Format | Notes |
|---|---|---|
| Hacker News (Show HN) | "Show HN: grelmicro — async microservice toolkit for FastAPI" | Post in the morning ET. Link the repo, not a blog. Be present for the first 2 hours. |
| r/Python | Text post | Lead with the problem (distributed primitives for FastAPI), then the demo. Flair: "Show and Tell". |
| r/FastAPI | Text post | Focus on the FastAPI integration and the demo. |
| dev.to | Article | "Distributed primitives for FastAPI without the boilerplate". Embed the demo asset. |
| X / Bluesky | Thread | One Pattern per post with a code snippet; end with the repo link. |

Draft copy lives in this file's history; refine per channel. Do not cross-post the same text verbatim.

## Badges (#177)

Add to the top of `README.md` once enabled:

```markdown
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/grelinfo/grelmicro/badge)](https://securityscorecards.dev/viewer/?uri=github.com/grelinfo/grelmicro)
[![SLSA 3](https://slsa.dev/images/gh-badge-level3.svg)](https://slsa.dev)
```

To make them real:

1. **OpenSSF Scorecard**: add the official `ossf/scorecard-action` workflow (`.github/workflows/scorecard.yml`) running on a weekly `schedule` and on push to `main`, with `publish_results: true`. It needs `id-token: write` and `security-events: write` permissions. The badge endpoint goes live after the first successful run. Keep it off pull-request triggers so it never gates PR CI.
2. **SLSA provenance**: have the release workflow generate provenance with `slsa-framework/slsa-github-generator` and attach it to the GitHub Release + PyPI (Trusted Publishing with attestations). The SLSA badge is static once provenance ships.

Both need the actions pinned by commit SHA (the repo's `zizmor` workflow-lint enforces this).

## Demo asset (#178)

Record a ~30s [asciinema](https://asciinema.org) cast of the demo, then embed it:

```bash
cd examples/fastapi-demo
asciinema rec demo.cast
# in the recording: docker compose up --wait, curl a couple endpoints, ctrl-d
```

Upload the cast (asciinema.org or an SVG via `svg-term`) and add it to the top of `examples/fastapi-demo/README.md` and the main README "Run the demo" section. A GIF works too; keep it under ~2 MB so GitHub renders it inline.

## Launch post drafts (#172)

Ready-to-post copy for each channel. Adjust the version number and demo link before publishing. Do not cross-post the same text verbatim.

---

### Show HN

**Title (79 chars):** `Show HN: grelmicro 1.0 - async microservice toolkit for Python`

**First comment (from the author, post within 5 minutes):**

> I kept re-implementing the same five patterns across every async Python service at work: a distributed lock, a rate limiter, a circuit breaker, a cache with stampede protection, and health check endpoints. Each one pulled in a separate library with its own config format, its own backend client, its own lifecycle hooks.
>
> grelmicro bundles them all behind one unified API. One `Grelmicro(uses=[...])` container manages the lifecycle. One Redis client is shared by the cache, the lock, the rate limiter, and the circuit breaker. One `reconfigure(new_config)` call changes thresholds at runtime without a restart.
>
> It is not a task queue and not a web framework. It is the layer between "I picked FastAPI" and "I need distributed coordination."
>
> Backends: Redis, PostgreSQL, SQLite, Kubernetes Lease, and in-memory (for tests). Patterns: Lock, TaskLock, LeaderElection, Cache, RateLimiter, CircuitBreaker, Retry, Bulkhead, Fallback, Timeout, HealthChecks, scheduled tasks (interval and cron).
>
> The [FastAPI demo](https://github.com/grelinfo/grelmicro/tree/main/examples/fastapi-demo) starts with `docker compose up --wait` and shows every pattern running against real Redis and Postgres.
>
> Happy to answer questions about design decisions, especially the backend-agnostic protocol model and the ambient resolution pattern.

---

### r/Python

**Title:** `grelmicro 1.0: one async toolkit for distributed locks, rate limits, circuit breakers, cache, cron, and health checks`

**Body:**

grelmicro 1.0 is out. It is an async Python toolkit that ships the microservice patterns you keep reimplementing, behind a unified API.

What it covers: distributed locks and leader election, cache with stampede protection and serve-stale-on-error, rate limiting (token bucket and sliding window), circuit breakers with live reconfiguration, idempotency keys, interval and cron scheduled tasks, structured JSON logging, OpenTelemetry tracing, and health check endpoints (`/livez`, `/readyz`, `/healthz`).

Backends for each pattern: Redis, PostgreSQL, SQLite, Kubernetes Lease, and in-memory for tests. Swap the backend without changing application code.

Why not separate libraries? If you only need one pattern, a focused library is often the right pick. The comparison page names the right one per domain. When you need two or more in the same service, grelmicro removes four or five separate config dialects and replaces them with one lifecycle (`async with micro:`) and one shared backend client.

- Repo: https://github.com/grelinfo/grelmicro
- Docs: https://grelinfo.github.io/grelmicro/
- Demo: https://github.com/grelinfo/grelmicro/tree/main/examples/fastapi-demo
- Comparison: https://grelinfo.github.io/grelmicro/comparison/

`pip install grelmicro`

---

### r/FastAPI

**Title:** `grelmicro 1.0: locks, rate limits, circuit breakers, cache, and health checks for FastAPI - one toolkit, one lifespan`

**Body:**

grelmicro 1.0 is a toolkit built for FastAPI and any asyncio app that needs distributed coordination.

The FastAPI integration is three lines: wire `Grelmicro(uses=[...])` into your lifespan and add `GrelmicroMiddleware`. After that, `Lock("cart")`, `RateLimiter.sliding_window(...)`, and `@cached(ttl_cache)` resolve ambiently in request handlers with no explicit `backend=` argument.

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with micro:
        yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(GrelmicroMiddleware, micro=micro)
```

Health checks are a FastAPI router you include with one line. It exposes the orchestrator-grade triple your Kubernetes deployment expects: `/livez` (shallow), `/readyz` (deep, concurrent checks, per-check timeout and TTL cache), and `/healthz` (aggregate).

The [FastAPI demo](https://github.com/grelinfo/grelmicro/tree/main/examples/fastapi-demo) shows every pattern in one `app.py`: cached endpoint, rate-limited endpoint, circuit-breaker-protected endpoint, distributed lock, leader-gated task, and health probes. Start it with `docker compose up --wait`.

What it is not: not a task queue (reach for Celery, Dramatiq, or taskiq), not a web framework (it plugs into FastAPI, not next to it).

- Repo: https://github.com/grelinfo/grelmicro
- Docs: https://grelinfo.github.io/grelmicro/
- Demo: https://github.com/grelinfo/grelmicro/tree/main/examples/fastapi-demo

`pip install grelmicro`

---

### dev.to

**Title:** `What "distributed" actually means in a FastAPI app`

**Tags:** `python, fastapi, microservices, opensource`

**Outline:**

1. The problem: four separate libraries, four config formats, four backend clients
2. What grelmicro ships (one-paragraph overview of each pattern group)
3. The FastAPI wiring pattern (lifespan + middleware, code snippet)
4. Cache with serve-stale-on-error (real problem, real code)
5. Rate limiting with retry-after headers (real code)
6. Distributed lock across replicas (real code)
7. Health check triple for Kubernetes
8. "When not to use grelmicro" (task queues, single-pattern use cases)
9. Getting started

**Intro paragraphs:**

Every FastAPI service past a certain size ends up with the same gap. You have a web framework for routing, a database driver for storage, and then a pile of small distributed coordination problems: rate limiting per user, a shared cache that does not stampede under load, a lock so two workers do not process the same job, and a circuit breaker so one slow downstream does not take down the rest.

Most teams reach for a separate library for each one. That works until you have four of them. At that point you are managing four config formats, four backend clients, and four different startup and shutdown sequences.

grelmicro is one async Python toolkit that covers all of these patterns behind a uniform API. One container manages the lifecycle. One Redis connection is shared by the cache, the lock, and the rate limiter. One `reconfigure(new_config)` call changes thresholds at runtime without a restart.

In this article I will show what the wiring looks like in a real FastAPI app and where grelmicro earns its place, including the honest "when not to use it" section.

**Honest "when not to use it" one-liners:**

- Use `tenacity` if retry is the only pattern you need.
- Use `aiocache` or `fastapi-cache` if cache is the only pattern you need.
- Use `slowapi` if per-route rate limiting with a wide backend choice is the only thing missing.
- Reach for `aioredlock` if you want Redlock specifically.
- Pick grelmicro when you need two or more of these in the same service and want one config and lifecycle story across all of them.
