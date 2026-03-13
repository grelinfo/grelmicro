from grelmicro.sync import Lock

lock = Lock("resource_name")


async def main():
    async with lock:
        print("Protected resource accessed")
