from pydantic import BaseModel

from grelmicro import Grelmicro
from grelmicro.idempotency import Idempotency
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis])


class Charge(BaseModel):
    amount: int


idem = Idempotency[Charge]("charge", ttl=3600)


async def do_charge(amount: int) -> Charge:
    return Charge(amount=amount)


async def main() -> None:
    async with micro:
        # The factory runs only on a first call, then the response replays.
        response = await idem.run("key-1", lambda: do_charge(100))
        print(response)
