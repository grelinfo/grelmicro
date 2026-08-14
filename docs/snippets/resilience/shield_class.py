import httpx
from pydantic import BaseModel

from grelmicro.resilience import Shield

github = Shield.api(
    "github",
    timeout_errors=(httpx.TimeoutException, httpx.ConnectError),
)


class Repo(BaseModel):
    name: str


@github
async def list_repos() -> list[Repo]:
    return []


@github
async def get_repo() -> Repo:
    return Repo(name="grelmicro")
