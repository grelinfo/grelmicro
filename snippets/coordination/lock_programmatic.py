from grelmicro.coordination import Lock

lock = Lock("cart", lease_duration=60, retry_interval=0.1)


async def main():
    async with lock:
        print("Protected resource accessed")
