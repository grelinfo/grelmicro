from fastapi import FastAPI, Request
from pydantic import BaseModel

from grelmicro.security import TrustedProxies, resolve_client_address

app = FastAPI()

# Your own proxies. Required, and there is no wildcard.
trusted = TrustedProxies(["10.0.0.0/8"])


class Who(BaseModel):
    client: str
    reason: str | None = None


@app.get("/whoami")
async def whoami(request: Request) -> Who:
    client = resolve_client_address(request.scope, trusted)
    if client is None:
        return Who(client="unknown")
    # `key` is safe to store or to use as a rate limiter bucket.
    return Who(client=client.key, reason=client.reason)
