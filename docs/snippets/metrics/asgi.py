from litestar import Litestar
from litestar.handlers import asgi

from grelmicro import Grelmicro
from grelmicro.metrics import Metrics, MetricsExporterType, metrics_asgi

micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.PROMETHEUS)])

app = Litestar(
    route_handlers=[asgi("/", is_mount=True, copy_scope=True)(metrics_asgi())]
)
micro.install(app)
# Endpoint: GET /metrics
