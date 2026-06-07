import asyncio

from grelmicro import Grelmicro
from grelmicro.metrics import Metrics, MetricsExporterType, measure


@measure(name="orders.process")
async def process(order_id: str) -> None:
    pass


micro = Grelmicro(
    uses=[
        Metrics(
            service_name="orders",
            exporter=MetricsExporterType.CONSOLE,
            export_interval=5,
        ),
    ]
)


async def main() -> None:
    async with micro:
        await process(order_id="ORD-1")
        micro.metrics.counter("orders.placed", unit="1").add(1)


asyncio.run(main())
