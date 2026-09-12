import os
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from grelmicro.security import (
    JWTConfig,
    JWTKey,
    JWTVerifier,
    TokenRejectedError,
)

app = FastAPI()

# Naming an audience or an issuer requires that claim on every token, so a
# token that simply omits it does not slip past the check.
verifier = JWTVerifier(
    JWTConfig(
        keys=[
            JWTKey(
                algorithm="RS256",
                key=os.environ["JWT_PUBLIC_KEY"].encode(),
                kid="2026-09",
            )
        ],
        audience=["grelmicro-api"],
        issuer=["https://auth.example.com/"],
    )
)


class Caller(BaseModel):
    subject: str
    scopes: list[str]


def current_caller(
    authorization: Annotated[str, Header()] = "",
) -> Caller:
    try:
        claims = verifier.verify_header(authorization)
    except TokenRejectedError as error:
        # The reason is a stable tag and never quotes the token, so it is
        # safe to hand back and safe to log.
        raise HTTPException(
            status_code=401,
            detail=error.reason,
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    return Caller(subject=claims.subject or "", scopes=sorted(claims.scopes))


@app.get("/orders")
async def list_orders(
    caller: Annotated[Caller, Depends(current_caller)],
) -> Caller:
    return caller
