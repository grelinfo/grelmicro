from fastapi import FastAPI, Request

from grelmicro.clientip import TrustedProxies, resolve_client_address

app = FastAPI()

# Your own proxies. Required, and there is no wildcard.
trusted = TrustedProxies(["10.0.0.0/8"])


@app.get("/whoami")
async def whoami(request: Request) -> dict[str, str]:
    client = resolve_client_address(request.scope, trusted)
    if client is None:
        return {"client": "unknown"}
    # `key` is safe to store or to use as a rate limiter bucket.
    return {"client": client.key, "reason": client.reason}
