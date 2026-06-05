from decimal import Decimal

import httpx

from grelmicro.cache import TTLCache
from grelmicro.resilience import shield


@shield.api(
    "prices",
    timeout_errors=(httpx.TimeoutException,),
    cache=TTLCache(ttl=300),
)
async def fetch_price(symbol: str) -> Decimal:
    return Decimal(0)
