"""PostgreSQL Backend.

To use this backend, please install `grelmicro[postgres]` or manually install `asyncpg`.
"""

from grelmicro.backends.postgres.lock import PostgresLockBackend

__all__ = ["PostgresLockBackend"]
