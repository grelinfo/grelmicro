from fastapi import FastAPI

from grelmicro.security import JWKSConfig, JWKSVerifier
from grelmicro.task import Tasks

app = FastAPI()
tasks = Tasks()

# The keys come from the provider, and the provider rotates them.
verifier = JWKSVerifier(
    JWKSConfig(
        url="https://auth.example.com/.well-known/jwks.json",
        audience=["grelmicro-api"],
        issuer=["https://auth.example.com/"],
    )
)


# Refreshing is the only part that talks to the network, and it happens here
# rather than on a request. It fetches only when the keys are stale, so
# calling it often costs nothing.
@tasks.every(seconds=60)
async def reload_signing_keys() -> None:
    await verifier.refresh()
