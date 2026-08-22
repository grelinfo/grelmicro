import httpx

from grelmicro.resilience import Retry, Stack, Timeout

NAME = "recs"

recs = Stack(
    NAME,
    patterns=[
        Retry.exponential(NAME, when=httpx.HTTPError, attempts=3),
        Timeout(NAME, seconds=1.0),
    ],
)


async def main(client: httpx.AsyncClient) -> httpx.Response:
    return await recs.run(client.get, "/recs/42")
