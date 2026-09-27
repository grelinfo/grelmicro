from grelmicro import Grelmicro
from grelmicro.coordination import Coordination, Lock
from grelmicro.coordination.memory import MemoryLockAdapter

backend = MemoryLockAdapter()
micro = Grelmicro(uses=[Coordination(lock=backend, requires="process")])
lock = Lock("cart", backend=backend)
