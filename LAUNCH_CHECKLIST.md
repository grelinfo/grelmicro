# grelmicro launch checklist

Maintainer planning doc (not published to the docs site). Tracks the launch tasks that need a human: posting, badges, and the demo recording. Issues [#172](https://github.com/grelinfo/grelmicro/issues/172), [#177](https://github.com/grelinfo/grelmicro/issues/177), [#178](https://github.com/grelinfo/grelmicro/issues/178).

## Pre-launch (do before posting)

The launch ships on the current `0.x` line. It is not gated on a 1.0 bump.

- [x] Tag and publish the launch release on PyPI. Confirm `pip install grelmicro` resolves it. Verified 2026-08-02: 0.34.1 is published, `pip install grelmicro` resolves it in a clean environment, and `just verify-release 0.34.1` confirms build provenance on both the wheel and the sdist. 0.34.0 was tagged the same day but never reached PyPI, because its release run failed on the Python 3.14 matrix before the publish step.
- [x] `docs/benchmarks.md` numbers dated, with the hardware, interpreter and machine conditions stated next to them. Re-measured 2026-10-22 on CPython 3.14.7 (grelmicro 0.42.4), best of three on a shared developer machine. The lock row is dropped: a release now finishes in a task of its own, so the cycle measures one event loop turn. The JWT claim links its measurements and `benchmarks/jwt_benchmark.py`.
- [x] The [FastAPI demo](examples/fastapi-demo) starts from a fresh clone in three commands. Verified 2026-07-28: `cd examples/fastapi-demo`, `docker compose up --wait`, `open http://localhost:8000/docs`.
- [x] Record the demo asset (see below) and embed it in the README. `docs/img/demo.gif`, 840K, embedded at the top of `README.md`.
- [x] Enable the badges (see below). Scorecard and SLSA 2 are both live in `README.md`. 0.32.5 is the first release publishing a verified attestation.
- [x] README first screen leads with the wedge: "Your FastAPI service, correct on more than one replica." It says Python 3.14+ and carries six badges.
- [x] The comparison page opens with the multi-replica case and shows `micro.install(app)`.
- [x] `[project.urls]` links the changelog.
- [x] CHANGELOG `Unreleased` section moved under the release version heading.

## Launch channels (#172)

Lead with one wedge: the scheduled job that runs once across replicas, then the rate limit, lock and circuit breaker that every replica shares. Breadth is the second message, never the lead. Say Python 3.14+ in the first lines of every post.

Post on one day, in this order, and stay on each thread:

| When (ET) | Channel | Format | Notes |
|---|---|---|---|
| Tue or Wed, 08:00 to 09:00 | Hacker News (Show HN) | Repo link, first comment within 5 minutes | Be present for the first hours. Never ask for votes. |
| Same day, 11:00 | r/Python | Text post, "Showcase" flair | Three headings: What My Project Does, Target Audience, Comparison. Check the sidebar rules first. |
| Same day, 14:00 | r/FastAPI | Story post | The two-terminal demo, one GIF. |
| Same day, 15:00 | FastAPI and Litestar Discord showcase channels | Short note | Link the repo and the demo. |
| Day 2 | PyCoder's Weekly, Python Trending Weekly | Submission | Blurbs below. |
| Day 2 | Bluesky, Mastodon, X | Thread | One pattern per post with a snippet, end with the repo link. |
| Day 3 | dev.to | Article | Outline below. |

Each channel gets its own angle. Do not cross-post the same text verbatim.

## Badges (#177)

**OpenSSF Scorecard**: live and in `README.md`. `scorecard.yml` runs
`ossf/scorecard-action` on a weekly schedule and on push to `main` with
`publish_results: true`, and stays off pull-request triggers so it never gates PR CI.

**OpenSSF Best Practices: passing at 100%.** [Project 13655](https://www.bestpractices.dev/projects/13655).
The last criterion to close was `report_process`, which needs a URL documenting how
users submit bug reports. `CONTRIBUTING.md#reporting-a-bug` is that URL, backed by the
GitHub issue forms under `.github/ISSUE_TEMPLATE/`. The self-certification is a
questionnaire, so a criterion that stops being true only goes stale if the answers are
never revisited. Re-check it when the reporting or release process changes.

**SLSA Build L2: live.** `release.yml` runs `actions/attest-build-provenance` on the
built artifacts, then `gh attestation verify` on each one before the release completes.
Provenance is signed on GitHub infrastructure separate from the build job, which is
what Level 2 asks for on a hosted runner.

The badge went into `README.md` only once a release actually backed it. **0.32.5 is the
first**, verified after publishing. 0.32.4 and earlier have no attestation, so the
badge would have claimed a level those artifacts could not back.

Check any published version with:

```bash
gh attestation verify <artifact> --repo grelinfo/grelmicro
```

**Staying conformant is enforced, not remembered.** The verify step fails the release
if provenance is missing or does not validate, so the claim cannot go stale while the
badge stays up. Removing the attest step breaks the next release rather than silently
turning the badge into a lie.

**Not Level 3.** Level 3 requires provenance generated by a reusable workflow the
build cannot influence, so that signing material is out of reach of user-defined build
steps. That is a separate change to the release path and is not done, so claim Level 2
only.

The PyPI Trusted Publishing attestations (`attestations: true`) sit alongside this and
prove origin, but PyPI assigns them no SLSA level on their own.

Any action added here must be pinned by commit SHA (the repo's `zizmor` workflow-lint
enforces this, and the repo sets `sha_pinning_required`).

## Demo asset (#178)

Record a ~30s [asciinema](https://asciinema.org) cast of the demo, then embed it:

```bash
cd examples/fastapi-demo
asciinema rec demo.cast
# in the recording: docker compose up --wait, curl a couple endpoints, ctrl-d
```

Upload the cast (asciinema.org or an SVG via `svg-term`) and add it to the top of `examples/fastapi-demo/README.md` and the main README "Run the demo" section. A GIF works too; keep it under ~2 MB so GitHub renders it inline.

## Launch post drafts (#172)

Ready-to-post copy for each channel. Adjust the version number and demo link before publishing.

---

### Show HN

**Title (75 chars):** `Show HN: Grelmicro, Python cron jobs, locks and rate limits across replicas`

Alternative: `Show HN: Grelmicro: make a FastAPI app safe to run on more than one replica`

**First comment (from the author, post within 5 minutes):**

> Hi HN, I am Loïc. I built grelmicro after hitting the same bug in every async Python service I shipped: it works on one replica and breaks quietly on two. The hourly job runs twice. The rate limit lets twice the traffic through. One copy keeps calling a dead downstream that the other copy already gave up on.
>
> grelmicro is a library, not a framework. You keep FastAPI (or Starlette, Litestar, FastStream). One line names where shared state lives: Redis, Valkey, PostgreSQL or SQLite. Then:
>
> - `@tasks.every(interval=60, gate="claim")` runs once per interval across all replicas,
> - `Lock`, `RateLimiter` and `CircuitBreaker` share their state between replicas,
> - health probes, logs, traces and metrics come wired for Kubernetes.
>
> Change the backend line and the rest of the code stays the same. A parity test checks every pattern on every framework. JWT verification runs in a small Rust core.
>
> Honest limits: Python 3.14+ only. Pre-1.0, so the API can still change on a minor release. Not a task queue (use Celery, Dramatiq or taskiq for durable jobs). If you only need retries, use tenacity or stamina.
>
> The demo starts with `docker compose up --wait` and runs against real Redis and Postgres. I would love feedback on the lease and fencing model behind the lock, and on what you would want before trusting it in production.

---

### r/Python

**Title:** `grelmicro: scheduled jobs, locks, rate limits and circuit breakers that stay correct across replicas (FastAPI, Litestar, asyncio)`

**Body:**

> **What My Project Does**
>
> grelmicro is an async Python library for the problems that appear when you run a second copy of your service. A scheduled job runs once per interval across all copies, not once per copy. A lock, a rate limit and a circuit breaker share state between copies. One line picks the backend: Redis, Valkey, PostgreSQL or SQLite.
>
> ```python
> from fastapi import FastAPI
>
> from grelmicro import Grelmicro
> from grelmicro.providers.redis import RedisProvider
> from grelmicro.task import Tasks
>
> app = FastAPI()
> tasks = Tasks()
> micro = Grelmicro(uses=[RedisProvider("redis://localhost:6379/0"), tasks])
> micro.install(app)
>
>
> @tasks.every(interval=60, gate="claim")
> async def send_report() -> None: ...
> ```
>
> **Target Audience**
>
> Teams running FastAPI, Starlette, Litestar or FastStream services on more than one replica, typically on Kubernetes. Python 3.14+. It is pre-1.0 (add a line on your own production use if true). The API can still change on a minor release.
>
> **Comparison**
>
> APScheduler runs a job in every process. slowapi and fastapi-limiter count per backend but stay FastAPI-only. pybreaker is sync-first. aioredlock is Redis-only and unmaintained since 2023. grelmicro covers these patterns with one provider and one lifecycle, on four backends. If you need only one pattern, the focused library is often the better pick, and the comparison page says which.
>
> - Repo: https://github.com/grelinfo/grelmicro
> - Docs: https://grelmicro.grel.info/
> - Demo: https://github.com/grelinfo/grelmicro/tree/main/examples/fastapi-demo
> - Comparison: https://grelmicro.grel.info/comparison/

---

### r/FastAPI

**Title:** `My FastAPI cron job ran on every replica. I wrote a library so it runs once.`

**Angle:** a story post. Show the two-terminal demo from the README and one GIF. Then "the same provider line also gives you a shared lock and circuit breaker", with the README's second example:

```python
from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.coordination import Lock
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import CircuitBreaker
from grelmicro.task import Tasks

app = FastAPI()
tasks = Tasks()
micro = Grelmicro(uses=[RedisProvider("redis://localhost:6379/0"), tasks])
micro.install(app)

sync_lock = Lock("inventory-sync")
warehouse = CircuitBreaker("warehouse")


@app.post("/inventory/sync")
async def sync_inventory() -> dict[str, str]:
    async with sync_lock, warehouse:
        return {"status": "synced"}  # your warehouse call
```

End with the Python 3.14+ caveat and the repo link.

---

### Newsletter blurbs

**PyCoder's Weekly** (project submission, 2 sentences):

> grelmicro: async Python patterns that stay correct across replicas. Scheduled jobs that run once, plus shared locks, rate limits and circuit breakers on Redis, PostgreSQL or SQLite, for FastAPI, Litestar and FastStream.

**Python Trending Weekly** (GitHub issue, 1 sentence):

> grelmicro makes FastAPI and Litestar services correct on more than one replica: one scheduled job run, one shared rate limit, one shared circuit breaker, on the database you already run.

**Talk Python / Python Bytes** (short email, after launch, only with a number to share): one paragraph with the wedge, the demo link, and one real lesson learned, for example the lease and fencing model.

---

### dev.to

**Title:** `Your FastAPI app is wrong on two replicas. Here is how to fix it.`

**Tags:** `python, fastapi, kubernetes, opensource`

**Outline:**

1. The double cron run: two replicas, one hourly job, two reports. The fix with `@tasks.every(interval=60, gate="claim")`.
2. The double rate limit: each replica counts its own budget. The fix with a `RateLimiter` on the shared backend.
3. The split-brain breaker: one replica keeps calling a dead downstream. The fix with a shared `CircuitBreaker`.
4. The wiring: one provider line and `micro.install(app)`, about 15 lines per fix.
5. When not to use grelmicro.
6. Getting started: the three-command demo.

**Honest "when not to use it" one-liners:**

- Use `tenacity` or `stamina` if retry is the only pattern you need.
- Use `aiocache` or `fastapi-cache` if a cache in one process is the only pattern you need.
- Use `slowapi` if per-route rate limiting is the only thing missing.
- Use a task queue (Celery, Dramatiq, taskiq) for durable jobs that survive a restart.
- Pick grelmicro when the service runs on more than one replica and a job, a limit or a breaker must hold across all of them.

---

### Launch-week rules

- Answer every HN and Reddit comment. Concede valid criticism in one line.
- Fix small reported bugs the same day and ship a patch on the current `0.x` line.
- Thank every first-time PR in public.
- Freeze breaking changes for two weeks after launch.
