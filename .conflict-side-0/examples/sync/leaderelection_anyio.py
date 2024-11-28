from anyio import create_task_group, sleep_forever

from grelmicro.sync.leaderelection import LeaderElection

leader = LeaderElection("cluster_group")


async def main():
    async with create_task_group() as tg:
        await tg.start(leader)
        await sleep_forever()
