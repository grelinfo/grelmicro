from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import (
    MemoryLeaderElectionAdapter,
    MemoryLockAdapter,
)

micro = Grelmicro(
    uses=[
        Coordination(
            lock=MemoryLockAdapter(),
            election=MemoryLeaderElectionAdapter(),
        )
    ]
)
