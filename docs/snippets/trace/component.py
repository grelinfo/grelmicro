import asyncio

from grelmicro import Grelmicro
from grelmicro.log import Log
from grelmicro.trace import Trace, TracingExporterType, instrument


@instrument
async def process(order_id: str) -> None:
    pass


micro = Grelmicro(
    uses=[
        Log(),
        Trace(
            service_name="orders",
            exporter=TracingExporterType.CONSOLE,
        ),
    ]
)


async def main() -> None:
    async with micro:
        await process(order_id="ORD-1")


asyncio.run(main())
