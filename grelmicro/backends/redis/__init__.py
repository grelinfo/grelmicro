"""Redis Backend.

To use this backend, please install `grelmicro[redis]` or manually install `redis`.
"""

from grelmicro.backends.redis.lock import RedisLockBackend

__all__ = ["RedisLockBackend"]
