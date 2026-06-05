from decimal import Decimal

import httpx

from grelmicro.cache import TTLCache
from grelmicro.resilience import shield


async def last_known_price(exc: Exception) -> Decimal:
    return Decimal(0)


@shield.api(
    "prices",
    timeout_errors=(httpx.TimeoutException,),
    cache=TTLCache(ttl=300),
    fallback=last_known_price,
)
async def fetch_price(symbol: str) -> Decimal:
    return Decimal(0)
