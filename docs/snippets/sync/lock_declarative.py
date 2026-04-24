from grelmicro.sync import Lock
from grelmicro.sync.lock import LockConfig

config = LockConfig(
    name="cart",
    worker="web-1",
    lease_duration=60,
    retry_interval=0.1,
)
lock = Lock("cart", config=config)


async def main():
    async with lock:
        print("Protected resource accessed")
