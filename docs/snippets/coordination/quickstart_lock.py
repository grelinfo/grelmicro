from grelmicro import Grelmicro
from grelmicro.coordination import Lock
from grelmicro.providers.memory import MemoryProvider

micro = Grelmicro(uses=[MemoryProvider()])

lock = Lock("cart")


async def checkout() -> None:
    async with lock:
        ...


async def main() -> None:
    # The lock resolves its backend inside the app scope.
    async with micro:
        await checkout()
