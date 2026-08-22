import httpx

from grelmicro.resilience import CircuitBreaker, Pattern, Retry, Stack, Timeout

NAME = "recs"

shared_backend = False

patterns: list[Pattern] = [
    Retry.exponential(NAME, when=httpx.HTTPError, attempts=3),
    Timeout(NAME, seconds=1.0),
]
if shared_backend:
    patterns.append(CircuitBreaker(NAME))

recs = Stack(NAME, patterns=patterns)
