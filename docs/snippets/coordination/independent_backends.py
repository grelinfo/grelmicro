from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination.kubernetes import KubernetesLeaderElectionBackend
from grelmicro.providers.redis import RedisProvider
from grelmicro.sync import Sync

redis = RedisProvider("redis://localhost:6379/0")

micro = Grelmicro(
    uses=[
        Sync(redis),  # Lock on Redis: low-latency mutual exclusion
        Coordination(  # leader election on a Kubernetes Lease
            KubernetesLeaderElectionBackend(namespace="default")
        ),
    ]
)
