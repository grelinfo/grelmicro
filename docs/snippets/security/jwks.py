from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from grelmicro.security import JWTVerifier

# The keys come from the provider, and the provider rotates them.
verifier = JWTVerifier.jwks(
    "https://auth.example.com/.well-known/jwks.json",
    audience="grelmicro-api",
    issuer="https://auth.example.com/",
)


# Opening the verifier loads the keys before the first request, and keeps
# them fresh in the background until the app stops. Nothing fetches on a
# request.
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with verifier:
        yield


app = FastAPI(lifespan=lifespan)
