# `Shield` algorithm spec

This page is the algorithmic specification for `Shield`. It defines two cooperating token buckets, the decision rules that connect them, and the **profile** — a named bundle of workload-dependent parameters that callers select instead of tuning numbers individually.

The implementation in `grelmicro/resilience/shield.py` and its private helpers conforms to this document. Changing a profile value or an algorithm rule means changing this page first.

## Signal model

`Shield` wraps a single async callable. The only signals available are:

- The callable **returned** (success).
- The callable **raised** an exception.
- The callable **exceeded** the per-attempt timeout (which surfaces as a `TimeoutError`).

There is no response envelope, no status code, no `Retry-After` header. Every classification in this spec is therefore expressed in terms of exception type, not response shape. Users who want HTTP-status-based behavior wrap their client so non-2xx responses raise (e.g. `httpx.Response.raise_for_status`).

## Two independent token buckets

`Shield` composes two unrelated token buckets. Either can be active without the other.

1. **Retry-budget bucket**: a single integer counter shared across retries on one `Shield` instance. Gates whether a retry is allowed at all.
2. **Adaptive rate bucket**: a per-second rate limiter applied to every outbound call. Stays inert until the dependency first signals slow-down, then engages and self-tunes with a CUBIC-style curve.

The retry-budget bucket is the "circuit breaker": when sustained failures drain it, retries are progressively suppressed without explicit open/half-open ceremony. The adaptive bucket is the "polite client": when the dependency keeps timing out or refusing connections, the whole client paces itself down and grows back gradually.

## Profile

A profile is a frozen bundle of workload-dependent parameters. One profile is selected per `Shield` instance. The profile is the **only** workload-tuning surface on `Shield`.

### Profile fields

| Field | Meaning | Layer |
|---|---|---|
| `max_consecutive_failures` | Retry-budget bucket capacity, in failures-without-recovery before retries are suppressed. | retry-budget |
| `initial_max_rate` | Tokens/second the adaptive bucket starts at, once enabled. | adaptive |
| `adaptive_burst_capacity` | Adaptive bucket capacity (maximum tokens it can hold; controls burst size). | adaptive |
| `min_rate_floor` | Lower bound enforced on the adaptive bucket's `max_rate`. | adaptive |
| `initial_timeout` | Per-attempt timeout used before the estimator has any sample. | timeout |
| `timeout_clamp_min` | Lower clamp on the adaptive per-attempt timeout. | timeout |
| `timeout_clamp_max` | Upper clamp on the adaptive per-attempt timeout. | timeout |
| `backoff_scale` | Backoff scale factor (seconds). Sized to dominate any inner-layer backoff envelope so the outer retries operate on a slower timescale. | backoff |
| `backoff_cap` | Hard cap on any single backoff delay, in seconds. | backoff |
| `max_rate_cap` | Optional hard ceiling on the adaptive bucket's `max_rate`, in tokens/second. `None` disables the cap (the default). Lets users enforce a contractual quota the dependency requires (e.g. "never exceed 10 req/s") even when CUBIC would otherwise raise the rate higher. | adaptive |

### Built-in profiles

Three profiles ship in the library. The default is `api`.

| Field | `internal` | `api` (default) | `slow` |
|---|---|---|---|
| `max_consecutive_failures` | `10` | `20` | `5` |
| `initial_max_rate` | `100/s` | `2/s` | `0.5/s` |
| `adaptive_burst_capacity` | `200` | `5` | `1` |
| `min_rate_floor` | `1/s` | `0.25/s` | `0.05/s` |
| `initial_timeout` | `1.0s` | `10.0s` | `120.0s` |
| `timeout_clamp_min` | `0.05s` | `0.5s` | `5.0s` |
| `timeout_clamp_max` | `5.0s` | `60.0s` | `600.0s` |
| `backoff_scale` | `0.5` | `1.0` | `2.0` |
| `backoff_cap` | `5s` | `30s` | `60s` |
| `max_rate_cap` | `None` | `None` | `None` |

Profile intent:

- **`internal`**: in-cluster RPC. Healthy services, fast latency, tight budgets. Fail fast and retry quickly.
- **`api`**: external HTTP APIs. Moderate latency, occasional outages, third-party SLAs. The default.
- **`slow`**: long-running calls — LLM inference, batch jobs, large queries. Each call is expensive, so the budget is small but timeouts and backoff are wide.

A profile is passed to `Shield` as a pre-built object, or selected by name via factory classmethod / decorator (`Shield.internal`, `Shield.api`, `Shield.slow`). Users may also construct a custom profile with explicit field values.

## Algorithm-level invariants

These do **not** vary by profile. They are properties of the algorithm itself.

| Constant | Value | Layer |
|---|---|---|
| Retry cost (uniform) | `1` | retry-budget |
| Successful-no-retry refund | `+1` | retry-budget |
| Successful-after-retry refund | `+1` per recovered retry | retry-budget |
| CUBIC scale constant `C` | `0.4` | adaptive |
| CUBIC multiplicative-decrease `β` | `0.7` | adaptive |
| Measured-rate clamp factor | `1.5` | adaptive |
| Max attempts | `4` (one initial + three retries) | retry policy |
| Timeout estimator window | last `32` successes | timeout |
| Timeout multiplier on p95 | `2.5` | timeout |
| Backoff family | exponential, full jitter | backoff |
| Backoff formula | `random_uniform(0, 1) · min(scale · 2^(i − 1), cap)` | backoff |

`β = 0.7` and `C = 0.4` are the values [RFC 9438](https://datatracker.ietf.org/doc/rfc9438/) gives for TCP CUBIC, validated by extensive deployment. They are **empirical defaults tuned for TCP congestion windows**, not first-principles constants. The cubic *shape* is RTT-invariant in TCP's domain; the C3 study ([NSDI 2015](https://www.usenix.org/conference/nsdi15/technical-sessions/presentation/suresh)) confirms a CUBIC-style controller is effective at the application layer when an explicit back-pressure signal is available. Applying these constants to application-level tokens-per-second is an engineering choice, not a theorem. They do not vary by profile because only the magnitudes (driven by profile fields like `initial_max_rate`) are workload-dependent — the curve shape itself is reused as-is. See the [References](#references) section.

## Retry-budget bucket

### Sizing

- The bucket is a simple consecutive-failure counter. Capacity equals the profile's `max_consecutive_failures`.
- Each retry costs **1 unit**. All retried failures are members of `timeout_errors`; there is no second class to price differently.
- Refunds are clamped at capacity.
- Scope: one bucket per `Shield` instance. Process-local. Protected by an `asyncio.Lock`.

### Refund rules

Evaluated on every attempt, before the next acquire:

| Outcome of attempt | Effect on budget |
|---|---|
| Failure (retryable) | acquire `1` (already done before the attempt, no additional action) |
| Success, no retry was performed on this call | refund `+1`, clamped to capacity |
| Success, after one or more retries | refund `+1` per recovered retry, clamped to capacity |

The "refund per recovered retry" rule is the key feedback loop. A transient outage that resolves itself returns the budget it consumed. A sustained outage drains it.

### Empty-budget behavior

When the bucket cannot cover the cost of a retry:

- The acquire call returns "denied". It does **not** raise.
- The retry loop ends silently. The underlying failure is surfaced to the caller as if the call had simply failed without retry.
- A debug-level log line is emitted.
- A PEP 678 note is added to the surfaced exception indicating the budget caused the stop.

### Call sequence

1. Call fires.
2. On failure, the retry policy first checks "is this retryable?". If yes:
   - The retry-budget bucket is asked for the cost.
   - On acquire-success: a backoff delay is computed, the loop sleeps, the call is retried.
   - On acquire-failure: the loop stops with the underlying failure.
3. After the final outcome is resolved, the refund rule above runs once.

## Adaptive rate bucket

### Lifecycle

- Created with `max_rate = initial_max_rate` (from the profile), capacity `adaptive_burst_capacity` (from the profile), starting empty.
- Capacity is the maximum number of tokens the bucket can hold at any time. It controls the burst size: how many calls can be made back-to-back after an idle period. `internal` allows large bursts (`200`) to support spiky in-cluster traffic, `api` allows small bursts (`5`) to respect external rate limits, `slow` allows essentially no bursts (`1`) since each call is expensive.
- A floor of `min_rate_floor` (from the profile) is enforced on `max_rate`.
- **Disabled until the first slow-down exception is observed.** While disabled it is a no-op: no acquire, no cost, no measurement gating. This keeps the adaptive layer invisible in the happy path.
- Once enabled, every outbound call performs a blocking acquire of `1` token before being sent.

### Blocking semantics

- Acquire is blocking by default: if the bucket lacks tokens, the caller waits while the bucket refills at `max_rate` tokens/second.
- The wait time for `n` tokens is `(n − current) / fill_rate`.
- The bucket refills passively on every state access. No background timer.

### CUBIC-style rate adjustment

The `max_rate` of the bucket is updated by a CUBIC-style controller parameterized by the two algorithm-level constants `C = 0.4` and `β = 0.7`.

The controller keeps three pieces of state:

- `w_max`: the rate at which the most recent slow-down was observed.
- `k`: the time offset such that the success curve passes through `w_max` at `t = k` since the last slow-down.
- `last_fail`: timestamp of the most recent slow-down.

**On a non-slow-down successful response (success path of the controller):**

The new candidate rate is computed from time since the last slow-down:

> `new_rate = C · (t_now − last_fail − k) ³ + w_max`

The curve starts below `w_max`, accelerates back toward it as time passes, then overshoots cautiously.

**On a slow-down exception:**

- `w_max ← current_rate`
- `k ← ((w_max · (1 − β)) / C) ^ (1/3)`
- `last_fail ← t_now`
- Returned candidate rate: `current_rate · β` (drop to 70% of the current rate).

**Clamp applied to the bucket:**

> `bucket.max_rate ← min(new_rate, 1.5 · measured_rate)`

The factor `1.5` caps how far ahead of the actual send rate the budget is allowed to run. `measured_rate` is a rolling client-side measurement of actual outbound rate, kept in a small rate-clocker auxiliary structure.

### The `timeout_errors` argument

Failures are classified by **exception type only**, against a single tuple `timeout_errors` passed to `Shield` at construction.

```python
@Shield.api(timeout_errors=(httpx.TimeoutException, httpx.ConnectError))
async def fetch(...): ...
```

**Default** when the argument is omitted:

```python
timeout_errors = (TimeoutError, asyncio.TimeoutError)
```

The default covers the per-attempt timeout `Shield` itself enforces (which always raises `TimeoutError`) and the standard Python timeout. Users wrapping a client library that raises its own timeout or connection types pass them explicitly. This is the *only* knob needed to teach `Shield` about a new client.

Concrete examples:

```python
# requests
@Shield.api(timeout_errors=(requests.Timeout, requests.ConnectionError))
def fetch(...): ...

# httpx
@Shield.api(timeout_errors=(httpx.TimeoutException, httpx.ConnectError))
async def fetch(...): ...

# boto3 (botocore)
@Shield.internal(timeout_errors=(botocore.exceptions.ReadTimeoutError,
                                    botocore.exceptions.ConnectTimeoutError,
                                    botocore.exceptions.EndpointConnectionError))
def get_object(...): ...

# netmiko (CLI over SSH to network gear — slow profile because commands routinely run 10–60s)
@Shield.slow(timeout_errors=(netmiko.exceptions.NetmikoTimeoutException,
                                netmiko.exceptions.ReadTimeout))
def run_command(...): ...
```

**Classification table** (this is the API contract):

| Raised by the wrapped call | Retried? | Triggers CUBIC shrink? | Consumes retry budget? |
|---|---|---|---|
| Any type in `timeout_errors` (or its subclasses) | yes | yes | yes |
| Any other `Exception` subclass | **no — propagates immediately** | no | no |
| Subclass of `ResilienceError` (our own signals) | **no — propagates immediately** | no | no |
| `BaseException` outside `Exception` (`KeyboardInterrupt`, `CancelledError`, `SystemExit`) | **no — propagates immediately** | no | no |

The user is in charge of declaring what "transient" means for their dependency. Anything not in `timeout_errors` is treated as a hard failure and surfaces unchanged.

`BaseException` propagation follows [PEP 654](https://peps.python.org/pep-0654/). On Python 3.14+, users mixing async generators with `Shield` should also be aware of [PEP 789](https://peps.python.org/pep-0789/), which restricts `yield` inside cancel scopes.

The retry loop is also bounded by `4` total attempts (algorithm invariant). The retry-budget bucket is the real gate; the attempts cap exists so a flaky call with a fresh budget cannot loop indefinitely.

### Mapping HTTP / gRPC status to `timeout_errors`

The spec is exception-only on purpose. Callers with a response envelope adapt at their own layer:

- **HTTP 429 / 503**: call `response.raise_for_status()` inside the wrapped function. The library you use (httpx, aiohttp) will raise its own exception type — pass that type in `timeout_errors`.
- **gRPC `RESOURCE_EXHAUSTED` / `UNAVAILABLE`**: the gRPC Python client raises `grpc.RpcError` subclasses. Pass them in `timeout_errors`.
- **`Retry-After` header**: not consumed by `Shield`. Read it inside the wrapped callable and `await asyncio.sleep(retry_after)` before raising. This preserves the "exception is the only signal" invariant.

### Composing with client-side retries (layered resilience)

`Shield` is designed as the **outer layer of resilience**. Many client libraries ship their own retry logic, tuned with knowledge their layer has: response envelopes, `Retry-After` headers, idempotency keys, modeled-retryable error codes. `Shield` does not replace that work. It adds a second, slower-timescale layer on top.

**The two layers operate on different timescales by design:**

| Layer | What it sees | What it does | Timescale |
|---|---|---|---|
| **Inner (client library)** | HTTP status codes, modeled errors, `Retry-After` headers, the full protocol | Short, dense retries that recover from per-call protocol-level transience | sub-second to seconds |
| **`Shield`** (outer) | Only the exception the inner layer *finally* surfaces after its own retries | Few, jittered retries with adaptive throttling — protects the application from sustained dependency failure | seconds to minutes |

Profile defaults assume one layer of inner retries below. The `backoff_scale` per profile is sized to operate on a slower timescale than typical inner-layer backoff envelopes, and `max_attempts = 4` adds only a few outer retries on top of whatever the inner layer already did.

**There is no need to disable the inner layer's retries.** The two layers compose. When the inner layer gives up, `Shield` sees the final exception and adds its own outer retries with CUBIC-paced delays. Inner-layer per-call recovery and `Shield`'s sustained-failure recovery handle different failure modes without conflict.

**What `Shield` sees:**

- The inner layer swallows transient 429s and connection blips it retries successfully. `Shield` never knows.
- Inner-layer `Retry-After` honoring stays correct.
- `Shield`'s adaptive bucket only engages when the inner layer has *exhausted* its own attempts — exactly when an application-level back-pressure response is warranted.

**The only thing you must do:** pass the inner layer's timeout / connection exception types via `timeout_errors=` so `Shield` recognizes them as slow-down signals.

## Backoff

Exponential with full jitter, capped at the profile's `backoff_cap`:

> `delay_i = random_uniform(0, 1) · min(profile.backoff_scale · 2^(i − 1), profile.backoff_cap)`

A single `backoff_scale` field per profile. Since every retried failure is a `timeout_errors` member, there is no second class of retryable failure to distinguish.

## Per-attempt timeout

`Shield` adds an adaptive per-attempt timeout because a one-line wrapper without a timeout is a footgun:

- A rolling estimator keeps the p95 of the last `32` successful latencies (power-of-two ring buffer).
- Per-attempt timeout `= p95 × 2.5`, clamped to `[timeout_clamp_min, timeout_clamp_max]` (from the profile).
- Initial value (before any samples): the profile's `initial_timeout`.
- The per-attempt `TimeoutError` is itself a `timeout_errors` member by default: a timeout triggers CUBIC shrink and consumes the retry budget like any other matched failure.

## Interaction between the two buckets

The buckets are independent and act at different layers:

- The **adaptive bucket** gates **every outbound call**, including first attempts. It paces the entire client during sustained pressure.
- The **retry-budget bucket** gates **only retries**. It limits how aggressively a single client retries during partial failures.

Concretely, in the failure path of one retried call:

1. Acquire on adaptive bucket (blocks if enabled).
2. Make the call inside the per-attempt timeout.
3. On failure: is the exception in `timeout_errors`?
4. If yes: update the CUBIC controller, shrink `max_rate`. Try to acquire from the retry-budget bucket. If denied: stop. If allowed: compute the backoff delay, sleep, loop.
5. If no: re-raise immediately. No retry, no CUBIC update.
6. On final resolution: refund the retry-budget bucket per the refund table.

## Scope and non-goals

A `Shield` instance is intended to wrap **one logical dependency**. Wrapping multiple unrelated dependencies under one `Shield` causes them to share a retry budget and a rate ceiling, which is rarely what the user wants. The library does not enforce this — it is a design recommendation users follow when constructing instances.

Explicit non-goals (each deserves its own primitive if needed):

- **Hedged / tied requests** ([Dean & Barroso 2013](https://dl.acm.org/doi/10.1145/2408776.2408794)). Hedging is a parallel-call race, not a retry loop, and belongs in a separate `Hedged` primitive.
- **Distributed retry budget** shared across processes. Per-instance, per-process only. A distributed variant would need to ride on the providers/Redis layer.
- **Deadline propagation** into the wrapped callable. The spec applies per-attempt timeouts only. Callers who need a total deadline wrap the whole `Shield` invocation in `asyncio.timeout(...)`.

## Known limitations (from the literature)

- **No coordinated convergence across instances.** Each `Shield` runs its own CUBIC controller. [RFC 9438 §5.6](https://datatracker.ietf.org/doc/rfc9438/) calls out that CUBIC convergence is slow "under low statistical multiplexing" — that is, when only a few clients share a bottleneck. Two `Shield` instances backing off in parallel may take longer than expected to settle.
- **No retry-rate-vs-base-traffic cap.** [Huang et al. (OSDI 2022)](https://www.usenix.org/conference/osdi22/presentation/huang-lexiang) found retry amplification accounts for >50% of metastable-failure sustaining effects. The spec's consecutive-failure counter bounds *consecutive* failures but does not directly bound the *ratio* of retries to base traffic. The adaptive bucket compensates indirectly by shrinking the call rate, but a future revision may add an explicit ratio cap.
- **The cubic-increase phase has no fairness guarantee.** [Chiu & Jain (1989)](https://doi.org/10.1016/0169-7552(89)90019-6) proved fairness only for AIMD's linear-increase phase. CUBIC's cubic-increase is a heuristic.

## References

Peer-reviewed, standards-track, and language-specification sources behind the design.

- Ha, S., Rhee, I., Xu, L. (2008). "CUBIC: A New TCP-Friendly High-Speed TCP Variant." *ACM SIGOPS OSR* 42(5). [DOI:10.1145/1400097.1400105](https://dl.acm.org/doi/10.1145/1400097.1400105) — the algorithm.
- Xu, L., Ha, S., Rhee, I., Goel, V., Eggert, L. (2023). [RFC 9438](https://datatracker.ietf.org/doc/rfc9438/), "CUBIC for Fast and Long-Distance Networks." — standards-track CUBIC, source of `C = 0.4` and `β = 0.7`.
- Chiu, D.-M., Jain, R. (1989). "Analysis of the Increase and Decrease Algorithms for Congestion Avoidance in Computer Networks." [DOI:10.1016/0169-7552(89)90019-6](https://doi.org/10.1016/0169-7552(89)90019-6) — AIMD optimality proof.
- Suresh, L., Canini, M., Schmid, S., Feldmann, A. (NSDI 2015). "C3: Cutting Tail Latency in Cloud Data Stores via Adaptive Replica Selection." [USENIX](https://www.usenix.org/conference/nsdi15/technical-sessions/presentation/suresh) — CUBIC applied at the application layer.
- Bronson, N., Aghayev, A., Charapko, A., Zhu, T. (HotOS 2021). "Metastable Failures in Distributed Systems." [DOI:10.1145/3458336.3465286](https://dl.acm.org/doi/10.1145/3458336.3465286) — model behind the retry-budget design.
- Huang, L., Magnusson, M., Muralikrishna, A., et al. (OSDI 2022). "Metastable Failures in the Wild." [USENIX](https://www.usenix.org/conference/osdi22/presentation/huang-lexiang) — empirical evidence that retry amplification is the dominant sustaining effect.
- Dean, J., Barroso, L. (2013). "The Tail at Scale." *CACM* 56(2). [DOI:10.1145/2408776.2408794](https://dl.acm.org/doi/10.1145/2408776.2408794) — tail-latency framing.
- Song, N., Kwak, B., Miller, L. (NIST JRES 2003). "On the Stability of Exponential Backoff." [PDF](https://nvlpubs.nist.gov/nistpubs/jres/108/4/j84son.pdf) — stability proof for exponential backoff.
- PEP 654, "Exception Groups and except*." [peps.python.org/pep-0654](https://peps.python.org/pep-0654/) — cancellation propagation semantics.
- PEP 678, "Enriching Exceptions with Notes." [peps.python.org/pep-0678](https://peps.python.org/pep-0678/) — how budget-exhaustion is annotated on surfaced exceptions.
- PEP 789, "Preventing `yield` inside Certain Context Managers." [peps.python.org/pep-0789](https://peps.python.org/pep-0789/) — async-generator cancellation safety, relevant for Python 3.14+ users.
