"""Trace component for the Grelmicro app object."""

from __future__ import annotations

import asyncio
import logging
import threading
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
from grelmicro.trace.errors import TracingError

if TYPE_CHECKING:
    from types import TracebackType


_logger = logging.getLogger(__name__)


class Trace:
    """Trace component: installs an OTel `TracerProvider` for the app's lifetime.

    Registered as `micro.trace` after `Grelmicro.use(Trace(...))`. On enter,
    builds a `TracerProvider` from the resolved config and installs it as the
    process-global provider. On exit, the provider is shut down and the
    previously-installed provider (if any) is restored.

    OTel's `set_tracer_provider` refuses to override an already-installed
    provider, so `Trace` writes the process-global directly. This means a
    single process should not run two `Grelmicro` apps with `Trace`
    components concurrently: their lifecycles share one OTel global.
    Sequential apps (the common test scenario) work fine.

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
    singleton: ClassVar[bool] = True

    def __init__(  # noqa: PLR0913
        self,
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. `Trace` installs the process-global OTel
                tracer provider, so only one may be registered per app.
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
        shutdown_timeout: Annotated[
            float | None,
            Doc(
                "Maximum seconds to wait for the `TracerProvider.shutdown()` "
                "flush. On timeout the call is abandoned (the daemon "
                "shutdown thread keeps running but cannot block loop "
                "teardown), a warning is logged, and the rest of "
                "`__aexit__` proceeds. Pending spans may be dropped."
            ),
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
        self._name = name
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
            "shutdown_timeout": shutdown_timeout,
        }
        self._env_load = env_load
        self._resolved: TracingConfig | None = None
        self._provider: Any = None
        self._prior_provider: Any = None

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

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
        # `opentelemetry.trace.set_tracer_provider()` refuses to replace an
        # already-installed provider, so `Trace` patches the private
        # `trace._TRACER_PROVIDER` global directly. A future OTel release
        # can rename or remove this attribute; the guard below turns that
        # into a clear error rather than a silent no-op patch.
        if not hasattr(trace, "_TRACER_PROVIDER"):
            msg = (
                "opentelemetry.trace no longer exposes `_TRACER_PROVIDER`. "
                "Trace relies on this private global to override the "
                "installed provider. Pin a compatible opentelemetry-api "
                "release or open an issue against grelmicro."
            )
            raise TracingError(msg)
        self._prior_provider = trace._TRACER_PROVIDER  # type: ignore[attr-defined]  # noqa: SLF001
        self._provider = _build_provider(self._resolved)
        trace._TRACER_PROVIDER = self._provider  # type: ignore[attr-defined]  # noqa: SLF001
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Shut down the provider and restore the prior global provider.

        `TracerProvider.shutdown()` blocks while the batch span processor
        flushes. A slow or broken exporter must not hang application
        shutdown, so the call runs in a daemon thread bounded by
        `shutdown_timeout`. The daemon thread sidesteps the default
        executor: if the timeout fires, the abandoned thread does not
        keep the asyncio loop alive on close, and is reaped at process
        exit. On timeout a warning is logged and the global provider is
        restored regardless.
        """
        from opentelemetry import trace  # noqa: PLC0415

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
                        "TracerProvider.shutdown timed out after %ss; "
                        "spans may be dropped.",
                        timeout,
                    )
        finally:
            trace._TRACER_PROVIDER = self._prior_provider  # type: ignore[attr-defined]  # noqa: SLF001
            self._provider = None
            self._prior_provider = None
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
            "TracerProvider.shutdown raised an exception; spans may be dropped",
            exc_info=captured[0],
        )
    return finished


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
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
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
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
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
    return OTLPSpanExporter(  # pragma: no cover
        **kwargs,  # ty: ignore[invalid-argument-type]
    )
