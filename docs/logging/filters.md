# Filters

Two `logging.Filter` implementations tame a logger that says the same thing too
often. Both attach to any stdlib logger, so they work with every
`GREL_LOG_BACKEND`.

For dropping Kubernetes probe lines from the access log, see
[Quieting health probes](integrations.md#quieting-health-probes).

## Deduplicating Noisy Logs

`DuplicateFilter` silences repeated log records.

```python
--8<-- "log/duplicate_filter.py"
```

After **5** identical records, the filter silently drops any further occurrences. It tracks up to **100** distinct keys in an LRU cache.

`key_mode="template"` (default) uses the raw format string as the key, so `%`-style calls with different arguments share one counter. It is also about **3 times faster** than rendered keying. `DuplicateFilter` and `RateLimitFilter` share the same five `key_mode` values:

| `key_mode` | Counter scope | Good for |
|---|---|---|
| `"logger"` | One counter per logger name | Collapse every record from a noisy logger |
| `"level"` | One counter per log level | Collapse all records at a level |
| `"global"` | One shared counter | Collapse everything into one budget |
| `"template"` (default) | One counter per (logger, level, `str(record.msg)`) | Shares across arg values of the same template |
| `"rendered"` | One counter per (logger, level, `record.getMessage()`) | Distinguishes fully-rendered messages |

Use `key_mode="rendered"` to track each rendered message separately, or pass `key=` for a custom fingerprint:

```python
logger.addFilter(DuplicateFilter(key_mode="rendered"))
logger.addFilter(DuplicateFilter(key=lambda r: (r.name, r.exc_info)))
```

Set `ttl` to re-emit a burst of `allowed_repetitions` records every window during sustained floods, so operators continue to receive periodic reminders:

```python
logger.addFilter(DuplicateFilter(allowed_repetitions=5, ttl=300))
```

State is in-process only. There is no cross-process sharing and no explicit reset API: construct a new filter if you need to wipe counters.

!!! tip
    For code using `from loguru import logger` or `structlog.get_logger()` directly, use those libraries' native filtering.

## Rate-Limiting Noisy Logs

`RateLimitFilter` drops records when a token bucket is empty. It allows bursts: up to `capacity` records can pass through at once, and the bucket then refills at `refill_rate` records per second.

```python
--8<-- "log/rate_limit_filter.py"
```

By default the filter buckets **per logger**: each logger has its own burst budget. Swap `key_mode` for different grouping:

| `key_mode` | Bucket scope | Good for |
|---|---|---|
| `"logger"` (default) | One bucket per logger name | Noisy third-party libraries that flood a single logger |
| `"level"` | One bucket per log level | Throttle all WARNING/ERROR across the app |
| `"global"` | One shared bucket | App-wide safety net on the root handler |
| `"template"` | One bucket per (logger, level, `str(record.msg)`) | Shares across arg values of the same template |
| `"rendered"` | One bucket per (logger, level, `record.getMessage()`) | Distinguishes fully-rendered messages |

```python
--8<-- "log/rate_limit_filter_global.py"
```

Pass a custom `key=` callable for any other grouping:

```python
logger.addFilter(
    RateLimitFilter(
        capacity=20,
        refill_rate=2,
        key=lambda r: f"{r.name}|{r.exc_info is not None}",
    )
)
```

Use `cost=` when a record should spend multiple tokens (e.g. on a verbose-level handler):

```python
logger.addFilter(RateLimitFilter(capacity=100, refill_rate=10, cost=2))
```

State is in-process only, backed by [`MemoryTokenBucket`][grelmicro.resilience.MemoryTokenBucket]. Call `filter.reset(key)` to clear one key, or construct a new filter to wipe all state.

!!! tip
    `RateLimitFilter` and `DuplicateFilter` compose well: attach the dedup filter first to collapse true duplicates, then the rate-limit filter to cap the sustained flow.

## Dropping Probe Noise

`ProbeFilter` drops a successful health probe from an access log, and keeps a
failing one, because a refused readiness check is often the only line saying
the kubelet asked. Attach it to the logger that writes them:

```python
import logging

from grelmicro.log import ProbeFilter

logging.getLogger("uvicorn.access").addFilter(ProbeFilter())
```

It matches by suffix, so a router mounted under a prefix is covered without
configuration, and takes `paths=` to name your own.

With [`AccessLog()`](access.md) registered, this is already the behaviour of
the record grelmicro writes, and uvicorn's access log is silenced, so the
filter is for an app that keeps uvicorn's.
