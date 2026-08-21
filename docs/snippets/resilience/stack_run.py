import httpx

from grelmicro.resilience import Retry, Stack, Timeout

recs = Stack(
    "recs",
    patterns=[
        Retry.exponential("recs", when=httpx.HTTPError, attempts=3),
        Timeout("recs", seconds=1.0),
    ],
)


async def main(client: httpx.AsyncClient) -> httpx.Response:
    return await recs.run(client.get, "/recs/42")
