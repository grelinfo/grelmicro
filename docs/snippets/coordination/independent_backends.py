from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination.kubernetes import KubernetesLeaderElectionAdapter
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")

micro = Grelmicro(
    uses=[
        Coordination(
            lock=redis,  # Lock on Redis: low-latency mutual exclusion
            election=KubernetesLeaderElectionAdapter(  # leader on a K8s Lease
                namespace="default"
            ),
        ),
    ]
)
