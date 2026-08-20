from grelmicro import Grelmicro
from grelmicro.cache import Cache, TTLCache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.coordination import Coordination, Lock
from grelmicro.coordination.memory import MemoryLockAdapter

cache_backend = MemoryCacheAdapter()
lock_backend = MemoryLockAdapter()

micro = Grelmicro(uses=[Cache(cache_backend), Coordination(lock=lock_backend)])

# Ambient: the backend is looked up on every operation.
sessions = TTLCache()
cart_lock = Lock("cart")

# Explicit: the backend is bound once, at construction.
hot_sessions = TTLCache(backend=cache_backend)
hot_cart_lock = Lock("cart", backend=lock_backend)
