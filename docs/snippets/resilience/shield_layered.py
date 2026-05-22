import httpx

from grelmicro.resilience import shield

retrying_httpx_client = httpx.AsyncClient()


@shield.api(
    "github",
    timeout_errors=(httpx.TimeoutException, httpx.ConnectError),
)
async def fetch(url: str) -> httpx.Response:
    return await retrying_httpx_client.get(url)
