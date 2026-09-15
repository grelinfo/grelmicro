from starlette.types import Scope

from grelmicro import Grelmicro
from grelmicro.http import AuthenticatedRequests, ErrorResponses
from grelmicro.providers.redis import RedisProvider
from grelmicro.security import JWTVerifier, Principal

redis = RedisProvider("redis://localhost:6379/0")


async def still_valid(caller: Principal, scope: Scope) -> Principal | None:
    token_id = caller.claims.get("jti")
    if token_id and await redis.client.exists(f"revoked:{token_id}"):
        return None
    signed_out = await redis.client.get(
        f"signed-out:{caller.issuer}:{caller.subject}"
    )
    issued_at = caller.claims.get("iat")
    if signed_out and (issued_at is None or issued_at <= int(signed_out)):
        return None
    return caller


micro = Grelmicro(
    uses=[
        redis,
        ErrorResponses(),
        AuthenticatedRequests(
            JWTVerifier.discover(
                "https://auth.example.com/", audience="orders-api"
            ),
            check=still_valid,
        ),
    ]
)
