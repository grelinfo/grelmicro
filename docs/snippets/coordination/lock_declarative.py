from grelmicro.coordination import Lock
from grelmicro.coordination.lock import LockConfig

config = LockConfig(
    worker="web-1",
    lease_duration=60,
    retry_interval=0.1,
)
lock = Lock.from_config("cart", config)


async def main():
    async with lock:
        print("Protected resource accessed")
