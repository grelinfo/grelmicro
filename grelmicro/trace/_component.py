"""Trace component for the Grelmicro app object."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro._config import resolve_config
from grelmicro.errors import DependencyNotFoundError
from grelmicro.trace.config import (
    TracingConfig,
    TracingExporterType,
    TracingProcessorType,
    TracingSamplerType,
)

if TYPE_CHECKING:
    from types import TracebackType


class Trace:
    """Trace component: installs an OTel `TracerProvider` for the app's lifetime.

    Registered as `micro.trace` after `Grelmicro.use(Trace(...))`. On enter,
    builds a `TracerProvider` from the resolved config and installs it via
    `opentelemetry.trace.set_tracer_provider`. On exit, the provider is shut
    down and the previously-installed provider (if any) is restored.

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.trace import Trace

        micro = Grelmicro(uses=[Trace(service_name="payments-api")])

        async with micro:
            ...
        ```

    The OTLP exporters are lazy-imported when selected. Install the matching
    exporter package: `opentelemetry-exporter-otlp-proto-http` or
    `opentelemetry-exporter-otlp-proto-grpc`.

    Read more in the [Tracing](../tracing.md) docs.
    """

    kind: ClassVar[str] = "trace"

    def __init__(  # noqa: PLR0913
        self,
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. Multiple `Trace` components may coexist
                on one `Grelmicro` under different names.
                """
            ),
        ] = "default",
        config: Annotated[
            TracingConfig | None,
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
            TracingExporterType | None, Doc("Span exporter.")
        ] = None,
        endpoint: Annotated[str | None, Doc("Exporter endpoint.")] = None,
        headers: Annotated[
            dict[str, str] | None, Doc("Exporter headers.")
        ] = None,
        processor: Annotated[
            TracingProcessorType | None, Doc("Span processor.")
        ] = None,
        sampler: Annotated[TracingSamplerType | None, Doc("Sampler.")] = None,
        sample_ratio: Annotated[
            float | None, Doc("Sample ratio for `traceidratio` sampler.")
        ] = None,
        resource_attributes: Annotated[
            dict[str, str] | None, Doc("Extra resource attributes.")
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                "Whether to read `GREL_TRACE_*` environment variables. "
                "When None (default), follow `GREL_ENV_LOAD`."
            ),
        ] = None,
    ) -> None:
        """Initialize the component (defer provider build until `__aenter__`)."""
        self.name = name
        self._explicit_config = config
        self._kwargs = {
            "service_name": service_name,
            "exporter": exporter,
            "endpoint": endpoint,
            "headers": headers,
            "processor": processor,
            "sampler": sampler,
            "sample_ratio": sample_ratio,
            "resource_attributes": resource_attributes,
        }
        self._env_load = env_load
        self._resolved: TracingConfig | None = None
        self._provider: Any = None
        self._prior_provider: Any = None

    @property
    def config(self) -> TracingConfig:
        """Return the resolved `TracingConfig`.

        Raises:
            RuntimeError: If accessed before the component has been entered.
        """
        if self._resolved is None:
            msg = "Trace.config is only available inside `async with micro:`"
            raise RuntimeError(msg)
        return self._resolved

    @property
    def provider(self) -> Any:  # noqa: ANN401
        """Return the installed OTel `TracerProvider`.

        Raises:
            RuntimeError: If accessed before the component has been entered.
        """
        if self._provider is None:
            msg = "Trace.provider is only available inside `async with micro:`"
            raise RuntimeError(msg)
        return self._provider

    async def __aenter__(self) -> Self:
        """Build the `TracerProvider` and install it as the global provider."""
        try:
            from opentelemetry import trace  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise DependencyNotFoundError(module="opentelemetry-api") from exc

        self._resolved = resolve_config(
            TracingConfig,
            explicit=self._explicit_config,
            kwargs=self._kwargs,
            env_prefix="GREL_TRACE_",
            env_load=self._env_load,
        )
        self._prior_provider = getattr(trace, "_TRACER_PROVIDER", None)
        self._provider = _build_provider(self._resolved)
        trace._TRACER_PROVIDER = self._provider  # type: ignore[attr-defined]  # noqa: SLF001
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Shut down the provider and restore the prior global provider."""
        from opentelemetry import trace  # noqa: PLC0415

        try:
            shutdown = getattr(self._provider, "shutdown", None)
            if callable(shutdown):
                shutdown()
        finally:
            trace._TRACER_PROVIDER = self._prior_provider  # type: ignore[attr-defined]  # noqa: SLF001
            self._provider = None
            self._prior_provider = None
        return None


def _build_provider(config: TracingConfig) -> Any:  # noqa: ANN401
    """Build a `TracerProvider` from a `TracingConfig`."""
    try:
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import (  # noqa: PLC0415
            BatchSpanProcessor,
            SimpleSpanProcessor,
        )
        from opentelemetry.sdk.trace.sampling import (  # noqa: PLC0415
            ALWAYS_OFF,
            ALWAYS_ON,
            ParentBased,
            TraceIdRatioBased,
        )
    except ImportError as exc:  # pragma: no cover
        raise DependencyNotFoundError(module="opentelemetry-sdk") from exc

    resource_attrs: dict[str, Any] = dict(config.resource_attributes)
    if config.service_name is not None:
        resource_attrs["service.name"] = config.service_name
    resource = Resource.create(resource_attrs) if resource_attrs else None

    if config.sampler == TracingSamplerType.ALWAYS_ON:
        sampler = ALWAYS_ON
    elif config.sampler == TracingSamplerType.ALWAYS_OFF:
        sampler = ALWAYS_OFF
    elif config.sampler == TracingSamplerType.TRACEIDRATIO:
        sampler = TraceIdRatioBased(config.sample_ratio)
    else:
        sampler = ParentBased(ALWAYS_ON)

    provider = TracerProvider(resource=resource, sampler=sampler)

    if config.exporter != TracingExporterType.NONE:
        exporter = _build_exporter(config)
        processor_cls = (
            BatchSpanProcessor
            if config.processor == TracingProcessorType.BATCH
            else SimpleSpanProcessor
        )
        provider.add_span_processor(processor_cls(exporter))

    return provider


def _build_exporter(config: TracingConfig) -> Any:  # noqa: ANN401
    """Build a span exporter for the configured exporter type."""
    if config.exporter == TracingExporterType.CONSOLE:
        from opentelemetry.sdk.trace.export import (  # noqa: PLC0415
            ConsoleSpanExporter,
        )

        return ConsoleSpanExporter()

    if config.exporter == TracingExporterType.OTLP_HTTP:  # pragma: no cover
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415  # ty: ignore[unresolved-import]
                OTLPSpanExporter,
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
        return OTLPSpanExporter(**kwargs)

    try:  # pragma: no cover
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415  # ty: ignore[unresolved-import]
            OTLPSpanExporter,
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
    return OTLPSpanExporter(**kwargs)  # pragma: no cover
