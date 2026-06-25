import asyncio
import signal

from grelmicro.task import Tasks

# Keep shutdown_timeout at or below the Kubernetes
# terminationGracePeriodSeconds (default 30s) so draining finishes
# before the pod receives SIGKILL.
tasks = Tasks(shutdown_timeout=25)


@tasks.every(seconds=5)
async def heartbeat() -> None:
    """Run periodic work; finishes the current run before shutdown."""


def _request_stop(stop: asyncio.Future[None]) -> None:
    """Resolve the stop future the first time a signal arrives."""
    if not stop.done():
        stop.set_result(None)


async def main() -> None:
    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    # Translate the orchestrator's signals into a future, then race it
    # against the workload. Leaving `async with tasks` drains the tasks.
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_stop, stop)
    async with tasks:
        await stop


if __name__ == "__main__":
    asyncio.run(main())
