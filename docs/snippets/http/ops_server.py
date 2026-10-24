from faststream import FastStream
from faststream.redis import RedisBroker

from grelmicro import Grelmicro
from grelmicro.health import HealthChecks
from grelmicro.http import OpsServer
from grelmicro.metrics import Metrics
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider(url="redis://localhost:6379/0")
health = HealthChecks(provider_checks="require")

micro = Grelmicro(
    uses=[
        redis,
        health,
        Metrics(exporter="prometheus"),
        OpsServer(port=8080),
    ]
)

app = FastStream(RedisBroker("redis://localhost:6379/0"))
micro.install(app)
# GET /livez, /readyz, /healthz and /metrics on :8080
