import httpx

from grelmicro.resilience import Shield

github = Shield.api(
    "github",
    timeout_errors=(httpx.TimeoutException, httpx.ConnectError),
)


@github
async def list_repos() -> list[dict]:
    return []


@github
async def get_repo() -> dict:
    return {}
