from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination.kubernetes import (
    KubernetesLeaderElectionAdapter,
    KubernetesLockAdapter,
)

micro = Grelmicro(
    uses=[
        Coordination(
            lock=KubernetesLockAdapter(namespace="default"),
            leaderelection=KubernetesLeaderElectionAdapter(namespace="default"),
        )
    ]
)
