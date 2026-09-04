from faststream import FastStream
from faststream.redis import RedisBroker

from grelmicro import Grelmicro
from grelmicro.health import HealthChecks
from grelmicro.http import OpsServer
from grelmicro.metrics import Metrics, MetricsExporterType
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider(url="redis://localhost:6379/0")
health = HealthChecks(auto_health=True)

micro = Grelmicro(
    uses=[
        redis,
        health,
        Metrics(exporter=MetricsExporterType.PROMETHEUS),
        OpsServer(port=8080),
    ]
)

app = FastStream(RedisBroker("redis://localhost:6379/0"))
micro.install(app)
# GET /livez, /readyz, /healthz and /metrics on :8080
