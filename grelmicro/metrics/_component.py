"""Metrics component for the Grelmicro app object."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro._config import resolve_config
from grelmicro.errors import DependencyNotFoundError
from grelmicro.metrics import _hub
from grelmicro.metrics.config import (
    MetricsConfig,
    MetricsExporterType,
)
from grelmicro.metrics.errors import (
    MetricsError,
    MetricsSettingsValidationError,
)

if TYPE_CHECKING:
    from types import TracebackType

    from opentelemetry.metrics import (
        Counter,
        Histogram,
        Meter,
        UpDownCounter,
    )
    from opentelemetry.metrics import (
        _Gauge as Gauge,
    )


_logger = logging.getLogger(__name__)


class Metrics:
    """Metrics component: installs an OTel `MeterProvider` for the app's lifetime.

    Registered as `micro.metrics` after `Grelmicro.use(Metrics(...))`. On
    enter, builds a `MeterProvider` from the resolved config and installs it
    as the process-global provider. On exit, the provider is shut down and the
    previously-installed provider (if any) is restored.

    OTel's `set_meter_provider` refuses to override an already-installed
    provider, so `Metrics` writes the process-global directly. This means a
    single process should not run two `Grelmicro` apps with `Metrics`
    components concurrently: their lifecycles share one OTel global.
    Sequential apps (the common test scenario) work fine.

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.metrics import Metrics

        micro = Grelmicro(uses=[Metrics(service_name="payments-api")])

        async with micro:
            ...
        ```

    The OTLP and Prometheus exporters are lazy-imported when selected.
    Install the matching exporter package:
    `opentelemetry-exporter-otlp-proto-http`,
    `opentelemetry-exporter-otlp-proto-grpc`, or
    `opentelemetry-exporter-prometheus`.

    Read more in the [Metrics](../metrics.md) docs.
    """

    kind: ClassVar[str] = "metrics"
    singleton: ClassVar[bool] = True

    def __init__(  # noqa: PLR0913
        self,
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. `Metrics` installs the process-global OTel
                meter provider, so only one may be registered per app.
                """
            ),
        ] = "default",
        config: Annotated[
            MetricsConfig | None,
            Doc(
                """
                Pre-built configuration. When provided, individual kwargs
                must be `None`. The env path is bypassed.
                """
            ),
        ] = None,
        service_name: Annotated[
            str | None, Doc("Service name resource attribute.")
        ] = None,
        exporter: Annotated[
            MetricsExporterType | None, Doc("Metric exporter.")
        ] = None,
        endpoint: Annotated[str | None, Doc("Exporter endpoint.")] = None,
        headers: Annotated[
            dict[str, str] | None, Doc("Exporter headers.")
        ] = None,
        export_interval: Annotated[
            float | None, Doc("Seconds between periodic exports.")
        ] = None,
        export_timeout: Annotated[
            float | None, Doc("Maximum seconds a single export may take.")
        ] = None,
        resource_attributes: Annotated[
            dict[str, str] | None, Doc("Extra resource attributes.")
        ] = None,
        shutdown_timeout: Annotated[
            float | None,
            Doc(
                "Maximum seconds to wait for the `MeterProvider.shutdown()` "
                "flush. On timeout the call is abandoned (the daemon "
                "shutdown thread keeps running but cannot block loop "
                "teardown), a warning is logged, and the rest of "
                "`__aexit__` proceeds. Pending metrics may be dropped."
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                "Whether to read `GREL_METRICS_*` environment variables. "
                "When None (default), follow `GREL_ENV_LOAD`."
            ),
        ] = None,
    ) -> None:
        """Initialize the component (defer provider build until `__aenter__`)."""
        self._name = name
        self._explicit_config = config
        self._kwargs = {
            "service_name": service_name,
            "exporter": exporter,
            "endpoint": endpoint,
            "headers": headers,
            "export_interval": export_interval,
            "export_timeout": export_timeout,
            "resource_attributes": resource_attributes,
            "shutdown_timeout": shutdown_timeout,
        }
        self._env_load = env_load
        self._resolved: MetricsConfig | None = None
        self._provider: Any = None
        self._prior_provider: Any = None
        self._prometheus_registry: Any = None
        self._meters: dict[str, Meter] = {}

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            MetricsConfig,
            Doc(
                """
                The pre-built metrics configuration.

                Use this path when the configuration is assembled at
                startup from a settings tree (for example YAML, Vault,
                or a `pydantic-settings` aggregator). The environment
                path is bypassed and the config is used as-is.
                """,
            ),
        ],
        *,
        name: Annotated[
            str,
            Doc("Registration name. Defaults to `'default'`."),
        ] = "default",
    ) -> Self:
        """Construct a `Metrics` from a pre-built `MetricsConfig`."""
        return cls(name=name, config=config)

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    @property
    def config(self) -> MetricsConfig:
        """Return the resolved `MetricsConfig`.

        Raises:
            RuntimeError: If accessed before the component has been entered.
        """
        if self._resolved is None:
            msg = "Metrics.config is only available inside `async with micro:`"
            raise RuntimeError(msg)
        return self._resolved

    @property
    def provider(self) -> Any:  # noqa: ANN401
        """Return the installed OTel `MeterProvider`.

        Raises:
            RuntimeError: If accessed before the component has been entered.
        """
        if self._provider is None:
            msg = (
                "Metrics.provider is only available inside `async with micro:`"
            )
            raise RuntimeError(msg)
        return self._provider

    @property
    def prometheus_registry(self) -> Any:  # noqa: ANN401
        """Return the Prometheus `CollectorRegistry` feeding `/metrics`.

        Only the `prometheus` exporter sets this. For every other exporter
        the value is `None`. The FastAPI `metrics_router` reads this registry
        to render the exposition format.
        """
        return self._prometheus_registry

    def meter(
        self,
        name: Annotated[
            str,
            Doc("Instrumentation scope name, usually the module name."),
        ],
    ) -> Meter:
        """Return an OTel `Meter` for `name`, cached per scope name.

        Raises:
            RuntimeError: If accessed before the component has been entered.
        """
        if self._provider is None:
            msg = "Metrics.meter is only available inside `async with micro:`"
            raise RuntimeError(msg)
        meter = self._meters.get(name)
        if meter is None:
            meter = self._provider.get_meter(name)
            self._meters[name] = meter
        return meter

    def counter(
        self,
        name: Annotated[str, Doc("Instrument name, e.g. `orders.placed`.")],
        *,
        unit: Annotated[str, Doc("Unit of measure, e.g. `1` or `By`.")] = "",
        description: Annotated[str, Doc("Human-readable description.")] = "",
    ) -> Counter:
        """Create (or reuse) a `Counter`. Monotonic, increase-only.

        Raises:
            RuntimeError: If accessed before the component has been entered.
        """
        return self.meter("grelmicro.metrics").create_counter(
            name, unit=unit, description=description
        )

    def histogram(
        self,
        name: Annotated[str, Doc("Instrument name, e.g. `request.duration`.")],
        *,
        unit: Annotated[str, Doc("Unit of measure, e.g. `s` or `By`.")] = "",
        description: Annotated[str, Doc("Human-readable description.")] = "",
    ) -> Histogram:
        """Create (or reuse) a `Histogram` for value distributions.

        Raises:
            RuntimeError: If accessed before the component has been entered.
        """
        return self.meter("grelmicro.metrics").create_histogram(
            name, unit=unit, description=description
        )

    def up_down_counter(
        self,
        name: Annotated[str, Doc("Instrument name, e.g. `queue.depth`.")],
        *,
        unit: Annotated[str, Doc("Unit of measure, e.g. `1`.")] = "",
        description: Annotated[str, Doc("Human-readable description.")] = "",
    ) -> UpDownCounter:
        """Create (or reuse) an `UpDownCounter` that can rise and fall.

        Raises:
            RuntimeError: If accessed before the component has been entered.
        """
        return self.meter("grelmicro.metrics").create_up_down_counter(
            name, unit=unit, description=description
        )

    def gauge(
        self,
        name: Annotated[str, Doc("Instrument name, e.g. `pool.size`.")],
        *,
        unit: Annotated[str, Doc("Unit of measure, e.g. `1`.")] = "",
        description: Annotated[str, Doc("Human-readable description.")] = "",
    ) -> Gauge:
        """Create (or reuse) a synchronous `Gauge` recording last-set values.

        Raises:
            RuntimeError: If accessed before the component has been entered.
        """
        return self.meter("grelmicro.metrics").create_gauge(
            name, unit=unit, description=description
        )

    async def __aenter__(self) -> Self:
        """Build the `MeterProvider` and install it as the global provider."""
        try:
            import opentelemetry.metrics._internal as otel_internal  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise DependencyNotFoundError(module="opentelemetry-api") from exc

        self._resolved = resolve_config(
            MetricsConfig,
            explicit=self._explicit_config,
            kwargs=self._kwargs,
            env_prefix="GREL_METRICS_",
            env_load=self._env_load,
            error_type=MetricsSettingsValidationError,
        )
        # `opentelemetry.metrics.set_meter_provider()` refuses to replace an
        # already-installed provider, so `Metrics` patches the private
        # `_METER_PROVIDER` global directly. A future OTel release can rename
        # or remove this attribute; the guard below turns that into a clear
        # error rather than a silent no-op patch.
        if not hasattr(otel_internal, "_METER_PROVIDER"):
            msg = (
                "opentelemetry.metrics no longer exposes `_METER_PROVIDER`. "
                "Metrics relies on this private global to override the "
                "installed provider. Pin a compatible opentelemetry-api "
                "release or open an issue against grelmicro."
            )
            raise MetricsError(msg)
        self._prior_provider = otel_internal._METER_PROVIDER  # noqa: SLF001
        self._provider, self._prometheus_registry = _build_provider(
            self._resolved
        )
        otel_internal._METER_PROVIDER = self._provider  # noqa: SLF001
        _hub.activate(self)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Shut down the provider and restore the prior global provider.

        `MeterProvider.shutdown()` blocks while the periodic reader flushes.
        A slow or broken exporter must not hang application shutdown, so the
        call runs in a daemon thread bounded by `shutdown_timeout`. On timeout
        a warning is logged and the global provider is restored regardless.
        """
        import opentelemetry.metrics._internal as otel_internal  # noqa: PLC0415

        try:
            shutdown = getattr(self._provider, "shutdown", None)
            if callable(shutdown):  # pragma: no branch
                timeout = (
                    self._resolved.shutdown_timeout
                    if self._resolved is not None
                    else 5.0
                )
                if not await _run_with_timeout(shutdown, timeout):
                    _logger.warning(
                        "MeterProvider.shutdown timed out after %ss; "
                        "metrics may be dropped.",
                        timeout,
                    )
        finally:
            _hub.deactivate(self)
            otel_internal._METER_PROVIDER = self._prior_provider  # noqa: SLF001
            self._provider = None
            self._prior_provider = None
            self._prometheus_registry = None
            self._meters = {}
        return None


async def _run_with_timeout(fn: Any, timeout: float) -> bool:  # noqa: ANN401, ASYNC109
    """Run a blocking `fn()` in a daemon thread, bounded by `timeout`.

    Returns `True` when the call completed in time, `False` on timeout.
    Exceptions raised by `fn` are captured and logged as a warning so
    they do not surface through Python's unhandled-exception hook from
    a background thread. The thread is a daemon so an abandoned-on-
    timeout shutdown call cannot block the asyncio loop's default-
    executor teardown or process exit.
    """
    done = threading.Event()
    captured: list[Exception] = []

    def _runner() -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            captured.append(exc)
        finally:
            done.set()

    threading.Thread(target=_runner, daemon=True).start()
    loop = asyncio.get_running_loop()
    finished = await loop.run_in_executor(None, done.wait, timeout)
    if finished and captured:
        _logger.warning(
            "MeterProvider.shutdown raised an exception; metrics may be dropped",
            exc_info=captured[0],
        )
    return finished


def _build_provider(config: MetricsConfig) -> tuple[Any, Any]:
    """Build a `MeterProvider` and optional Prometheus registry from config.

    Returns the provider and the Prometheus `CollectorRegistry` (or `None`
    for non-Prometheus exporters).
    """
    try:
        from opentelemetry.sdk.metrics import (  # noqa: PLC0415
            MeterProvider,
        )
        from opentelemetry.sdk.metrics.export import (  # noqa: PLC0415
            PeriodicExportingMetricReader,
        )
    except ImportError as exc:  # pragma: no cover
        raise DependencyNotFoundError(module="opentelemetry-sdk") from exc

    from grelmicro.metrics._resource import build_resource  # noqa: PLC0415

    resource = build_resource(
        service_name=config.service_name,
        resource_attributes=config.resource_attributes,
    )

    if config.exporter == MetricsExporterType.NONE:
        return _make_provider(MeterProvider, resource, readers=[]), None

    if config.exporter == MetricsExporterType.PROMETHEUS:
        reader, registry = _build_prometheus_reader()
        return _make_provider(MeterProvider, resource, readers=[reader]), (
            registry
        )

    exporter = _build_exporter(config)
    reader = PeriodicExportingMetricReader(
        exporter,
        export_interval_millis=config.export_interval * 1000.0,
        export_timeout_millis=config.export_timeout * 1000.0,
    )
    return _make_provider(MeterProvider, resource, readers=[reader]), None


def _make_provider(
    provider_cls: Any,  # noqa: ANN401
    resource: Any,  # noqa: ANN401
    *,
    readers: list[Any],
) -> Any:  # noqa: ANN401
    """Construct a `MeterProvider`, passing `resource` only when set."""
    kwargs: dict[str, Any] = {"metric_readers": readers}
    if resource is not None:
        kwargs["resource"] = resource
    return provider_cls(**kwargs)


def _build_prometheus_reader() -> tuple[Any, Any]:
    """Build a `PrometheusMetricReader` with a fresh `CollectorRegistry`."""
    try:
        from opentelemetry.exporter.prometheus import (  # noqa: PLC0415
            PrometheusMetricReader,
        )
        from prometheus_client import CollectorRegistry  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise DependencyNotFoundError(
            module="opentelemetry-exporter-prometheus"
        ) from exc

    registry = CollectorRegistry()
    reader = PrometheusMetricReader()
    # The reader registers its collector on the default Prometheus registry
    # by default. Re-register it on a dedicated registry so `/metrics`
    # exposes only this app's metrics and concurrent apps stay isolated.
    registry.register(reader._collector)  # noqa: SLF001
    return reader, registry


def _build_exporter(config: MetricsConfig) -> Any:  # noqa: ANN401
    """Build a metric exporter for the configured exporter type."""
    if config.exporter == MetricsExporterType.CONSOLE:
        from opentelemetry.sdk.metrics.export import (  # noqa: PLC0415
            ConsoleMetricExporter,
        )

        return ConsoleMetricExporter()

    if config.exporter == MetricsExporterType.OTLP_HTTP:  # pragma: no cover
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (  # noqa: PLC0415
                OTLPMetricExporter,
            )
        except ImportError as exc:
            raise DependencyNotFoundError(
                module="opentelemetry-exporter-otlp-proto-http"
            ) from exc
        kwargs: dict[str, Any] = {}
        if config.endpoint is not None:
            kwargs["endpoint"] = config.endpoint
        if config.headers:
            kwargs["headers"] = config.headers
        return OTLPMetricExporter(**kwargs)

    try:  # pragma: no cover
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (  # noqa: PLC0415
            OTLPMetricExporter,
        )
    except ImportError as exc:  # pragma: no cover
        raise DependencyNotFoundError(
            module="opentelemetry-exporter-otlp-proto-grpc"
        ) from exc
    kwargs = {}  # pragma: no cover
    if config.endpoint is not None:  # pragma: no cover
        kwargs["endpoint"] = config.endpoint
    if config.headers:  # pragma: no cover
        kwargs["headers"] = config.headers
    return OTLPMetricExporter(  # pragma: no cover
        **kwargs,  # ty: ignore[invalid-argument-type]
    )
