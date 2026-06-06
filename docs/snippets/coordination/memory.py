from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import (
    MemoryLeaderElectionBackend,
    MemoryLockAdapter,
)

micro = Grelmicro(
    uses=[
        Coordination(
            lock=MemoryLockAdapter(),
            election=MemoryLeaderElectionBackend(),
        )
    ]
)
