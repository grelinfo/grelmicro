import asyncio

from grelmicro import Grelmicro
from grelmicro.coordination import Lock
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis])


async def charge(order_id: str) -> None:
    lock = Lock(f"order:{order_id}", lease_duration=10)
    # Wait up to 5 seconds, then raise LockTimeoutError.
    async with lock.hold(timeout=5) as held:
        print(f"Charging order {order_id}, fencing token {held.fencing_token}")


async def main() -> None:
    async with micro:
        await charge("42")


if __name__ == "__main__":
    asyncio.run(main())
