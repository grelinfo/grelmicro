from grelmicro import Grelmicro
from grelmicro.coordination import Coordination, Lock
from grelmicro.coordination.memory import MemoryLockAdapter

micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])

lock = Lock("cart")


async def checkout() -> None:
    async with lock:
        ...
