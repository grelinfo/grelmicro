import httpx

from grelmicro.resilience import CircuitBreaker, Pattern, Retry, Stack, Timeout

shared_backend = False

patterns: list[Pattern] = [
    Retry.exponential("recs", when=httpx.HTTPError, attempts=3),
    Timeout("recs", seconds=1.0),
]
if shared_backend:
    patterns.append(CircuitBreaker("recs"))

recs = Stack("recs", patterns=patterns)
