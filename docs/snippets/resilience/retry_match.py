import httpx
from pydantic import BaseModel

from grelmicro.resilience import Match, retry


class Job(BaseModel):
    status: str


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
async def poll_job(client: httpx.AsyncClient, job_id: str) -> Job | None:
    response = await client.get(f"/jobs/{job_id}")
    job = Job.model_validate(response.json())
    return job if job.status == "ready" else None
