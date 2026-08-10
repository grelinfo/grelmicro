from grelmicro import Grelmicro
from grelmicro.coordination import Lock
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis])

lock = Lock("cart")


async def checkout() -> None:
    async with lock:
        ...


async def main() -> None:
    # The lock resolves its backend inside the app scope.
    async with micro:
        await checkout()
