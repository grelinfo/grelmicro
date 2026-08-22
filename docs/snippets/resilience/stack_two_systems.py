import httpx

from grelmicro.resilience import CircuitBreaker, Fallback, Stack, Timeout

SYSTEM = "recs-api"

# One breaker for the system, shared by every call site that reaches it.
breaker = CircuitBreaker(SYSTEM)

recs_list = Stack(
    "recs-list",
    patterns=[
        Fallback("recs-list", when=Exception, default=[]),
        breaker,
        Timeout("recs-list", seconds=1.0),
    ],
)

recs_report = Stack(
    "recs-report",
    patterns=[
        breaker,
        Timeout("recs-report", seconds=30.0),
    ],
)


@recs_list
async def get_recommendations(client: httpx.AsyncClient) -> list[str]:
    return (await client.get("/recs")).json()


@recs_report
async def build_report(client: httpx.AsyncClient) -> bytes:
    return (await client.get("/recs/report")).content
