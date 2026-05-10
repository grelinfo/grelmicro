import httpx

from grelmicro.resilience import Match, retry


# Compose exception and result matchers with `|`.
# Retry on transient HTTP errors OR when the response carries a
# server-side soft-fail marker.
@retry(
    when=Match.exception(httpx.HTTPError)
    | Match.result(lambda r: r.headers.get("X-Soft-Fail") == "true"),
    attempts=5,
)
async def fetch(client: httpx.AsyncClient, url: str) -> httpx.Response:
    return await client.get(url)


# Polling-style: retry until the result is no longer ``None``.
@retry(when=Match.result(None), attempts=20)
async def poll_job(client: httpx.AsyncClient, job_id: str) -> dict | None:
    response = await client.get(f"/jobs/{job_id}")
    payload = response.json()
    return payload if payload["status"] == "ready" else None
