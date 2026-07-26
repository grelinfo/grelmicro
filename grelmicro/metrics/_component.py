"""Metrics component for the Grelmicro app object."""

from __future__ import annotations

import asyncio
import logging
import os
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
        basic_auth: Annotated[
            tuple[str, str] | None,
            Doc(
                """
                HTTP Basic auth credentials as a `(username, password)`
                pair. grelmicro builds the `Authorization: Basic` header and
                attaches it to the OTLP exporter directly, so it never goes
                through the fragile `OTEL_EXPORTER_OTLP_HEADERS` encoding.
                From the environment, set `GREL_METRICS_BASIC_AUTH_USERNAME`
                and `GREL_METRICS_BASIC_AUTH_PASSWORD` instead.
                """
            ),
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
        if basic_auth is not None and len(basic_auth) != 2:  # noqa: PLR2004
            msg = "basic_auth must be a (username, password) tuple."
            raise TypeError(msg)
        self._name = name
        self._explicit_config = config
        self._kwargs = {
            "service_name": service_name,
            "exporter": exporter,
            "endpoint": endpoint,
            "headers": headers,
            "basic_auth_username": basic_auth[0] if basic_auth else None,
            "basic_auth_password": basic_auth[1] if basic_auth else None,
            "export_interval": export_interval,
            "export_timeout": export_timeout,
            "resource_attributes": resource_attributes,
            "shutdown_timeout": shutdown_timeout,
        }
        self._env_load = env_load
        self._resolved: MetricsConfig | None = None
        self._entered: bool = False
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
            RuntimeError: If accessed before the component has been entered,
                or when the exporter auto-disables so no provider is installed.
        """
        if self._provider is None:
            msg = (
                "Metrics.provider is only available inside `async with micro:` "
                "and only when the exporter is active. An auto-disabled Metrics "
                "(default exporter with no endpoint) installs no provider."
            )
            raise RuntimeError(msg)
        return self._provider

    @property
    def active(self) -> bool:
        """Whether entering installs a `MeterProvider` for this app.

        `False` when the exporter auto-disables: the default `auto` exporter
        with no endpoint configured. An auto-disabled `Metrics` is a no-op, so
        it can be registered unconditionally in dev, test, and CI.
        """
        return not self._auto_disabled()

    def owns_global_state(self) -> bool:
        """Whether entering patches the process-global meter provider.

        Consulted by the app's single-active-app guard. An auto-disabled
        `Metrics` installs nothing, so overlapping apps may each carry one.
        """
        return self.active

    def _auto_disabled(self) -> bool:
        """Return True when the exporter was left `auto` and resolves to `none`."""
        config = self._resolve()
        return (
            config.exporter is MetricsExporterType.AUTO
            and _resolve_exporter_type(config) is MetricsExporterType.NONE
        )

    def _resolve(self) -> MetricsConfig:
        """Resolve the config once per open cycle (cleared on exit)."""
        if self._resolved is None:
            self._resolved = resolve_config(
                MetricsConfig,
                explicit=self._explicit_config,
                kwargs=self._kwargs,
                env_prefix="GREL_METRICS_",
                env_load=self._env_load,
                error_type=MetricsSettingsValidationError,
            )
        return self._resolved

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

        When the exporter auto-disables (default `auto` exporter, no endpoint),
        no provider is installed and this returns a no-op `Meter` from the OTel
        global, so custom instruments stay safe to use unconditionally.

        Raises:
            RuntimeError: If accessed before the component has been entered.
        """
        if not self._entered:
            msg = "Metrics.meter is only available inside `async with micro:`"
            raise RuntimeError(msg)
        if self._provider is None:
            from opentelemetry import metrics  # noqa: PLC0415

            return metrics.get_meter(name)
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
        """Build the `MeterProvider` and install it as the global provider.

        When the exporter auto-disables (default `auto` exporter, no endpoint),
        this is a true no-op: no provider is built, the process-global meter
        provider is left untouched, and no instruments are emitted.
        """
        config = self._resolve()
        self._entered = True
        if not self.active:
            return self
        try:
            import opentelemetry.metrics._internal as otel_internal  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise DependencyNotFoundError(module="opentelemetry-api") from exc

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
        self._provider, self._prometheus_registry = _build_provider(config)
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
        if self._provider is None:
            # Auto-disabled on enter: nothing was installed, nothing to undo.
            self._entered = False
            self._resolved = None
            self._prior_provider = None
            return None

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
            self._entered = False
            self._resolved = None
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

    exporter_type = _resolve_exporter_type(config)

    if exporter_type == MetricsExporterType.NONE:
        return _make_provider(MeterProvider, resource, readers=[]), None

    if exporter_type == MetricsExporterType.PROMETHEUS:
        reader, registry = _build_prometheus_reader()
        return _make_provider(MeterProvider, resource, readers=[reader]), (
            registry
        )

    exporter = _build_exporter(config, exporter_type)
    reader = PeriodicExportingMetricReader(
        exporter,
        export_interval_millis=config.export_interval * 1000.0,
        export_timeout_millis=config.export_timeout * 1000.0,
    )
    return _make_provider(MeterProvider, resource, readers=[reader]), None


def _resolve_exporter_type(config: MetricsConfig) -> MetricsExporterType:
    """Resolve the `auto` exporter against the configured endpoint.

    `auto` becomes `otlp-http` when an endpoint is resolvable (the
    `endpoint` field, `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`, or
    `OTEL_EXPORTER_OTLP_ENDPOINT`) and `none` otherwise. Any explicit
    exporter is returned unchanged.
    """
    if config.exporter != MetricsExporterType.AUTO:
        return config.exporter
    endpoint_configured = (
        config.endpoint is not None
        or bool(os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"))
        or bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))
    )
    if endpoint_configured:
        return MetricsExporterType.OTLP_HTTP
    return MetricsExporterType.NONE


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


def _build_exporter(
    config: MetricsConfig,
    exporter_type: MetricsExporterType,
) -> Any:  # noqa: ANN401
    """Build a metric exporter for the resolved exporter type."""
    if exporter_type == MetricsExporterType.CONSOLE:
        from opentelemetry.sdk.metrics.export import (  # noqa: PLC0415
            ConsoleMetricExporter,
        )

        return ConsoleMetricExporter()

    if exporter_type == MetricsExporterType.OTLP_HTTP:  # pragma: no cover
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (  # noqa: PLC0415
                OTLPMetricExporter,
            )
        except ImportError as exc:
            raise DependencyNotFoundError(
                module="opentelemetry-exporter-otlp-proto-http"
            ) from exc
        return OTLPMetricExporter(**_exporter_kwargs(config))

    try:  # pragma: no cover
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (  # noqa: PLC0415
            OTLPMetricExporter,
        )
    except ImportError as exc:  # pragma: no cover
        raise DependencyNotFoundError(
            module="opentelemetry-exporter-otlp-proto-grpc"
        ) from exc
    return OTLPMetricExporter(  # pragma: no cover
        **_exporter_kwargs(config),
    )


def _exporter_kwargs(config: MetricsConfig) -> dict[str, Any]:
    """Build the shared `endpoint`/`headers` kwargs for the OTLP exporters.

    Merges any configured Basic-auth credentials into the headers as an
    `Authorization: Basic` value, so the credentials ride on the exporter
    rather than the env-parsed `OTEL_EXPORTER_OTLP_HEADERS`.
    """
    kwargs: dict[str, Any] = {}
    if config.endpoint is not None:
        kwargs["endpoint"] = config.endpoint
    headers = dict(config.headers)
    authorization = config.authorization_header
    if authorization is not None:
        headers["Authorization"] = authorization
    if headers:
        kwargs["headers"] = headers
    return kwargs
