import asyncio
import logging

from grelmicro import Grelmicro
from grelmicro.log import Log

micro = Grelmicro(uses=[Log()])


async def main() -> None:
    async with micro:
        logging.getLogger(__name__).info("hello", extra={"user_id": 123})


asyncio.run(main())
