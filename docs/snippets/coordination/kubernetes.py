from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination.kubernetes import (
    KubernetesLeaderElectionBackend,
    KubernetesLockAdapter,
)

micro = Grelmicro(
    uses=[
        Coordination(
            lock=KubernetesLockAdapter(namespace="default"),
            election=KubernetesLeaderElectionBackend(namespace="default"),
        )
    ]
)
