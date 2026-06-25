from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.idempotency import Idempotency

micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])

idem = Idempotency("charge", ttl=3600)


async def do_charge(amount: int) -> dict:
    return {"amount": amount}


async def main() -> None:
    async with micro:
        # The factory runs only on a first call, then the response replays.
        response = await idem.run("key-1", lambda: do_charge(100))
        print(response)
