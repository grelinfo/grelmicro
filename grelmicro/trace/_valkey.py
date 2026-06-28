"""First-party OpenTelemetry instrumentor for valkey-py.

`opentelemetry-instrumentation-redis` patches the `redis.*` classes only, so
valkey-py (the `valkey.*` redis-py fork) produces no spans through it. valkey-py
is structurally identical to redis-py, so this instrumentor applies that
package's command and pipeline span factories to the valkey client classes. The
spans are therefore byte-identical to the Redis spans an app already gets.

Registered as an `opentelemetry_instrumentor` entry point named `valkey`, so
`Trace(instrument=...)` discovers it through the same sweep as every other
library, and `ValkeyProvider` uses it directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

if TYPE_CHECKING:
    from collections.abc import Collection

_logger = logging.getLogger(__name__)

# Wrap targets mirroring `opentelemetry.instrumentation.redis._instrument`,
# applied to the `valkey.*` namespace. Each entry is (module, "Class.method").
# The "kind" picks which reused factory builds the wrapper: a command or a
# pipeline, sync or async.
_SYNC_COMMAND = "sync_command"
_SYNC_PIPELINE = "sync_pipeline"
_ASYNC_COMMAND = "async_command"
_ASYNC_PIPELINE = "async_pipeline"

_WRAP_TARGETS = (
    ("valkey", "Valkey.execute_command", _SYNC_COMMAND),
    ("valkey.client", "Pipeline.execute", _SYNC_PIPELINE),
    ("valkey.client", "Pipeline.immediate_execute_command", _SYNC_COMMAND),
    ("valkey.cluster", "ValkeyCluster.execute_command", _SYNC_COMMAND),
    ("valkey.cluster", "ClusterPipeline.execute", _SYNC_PIPELINE),
    ("valkey.asyncio", "Valkey.execute_command", _ASYNC_COMMAND),
    ("valkey.asyncio.client", "Pipeline.execute", _ASYNC_PIPELINE),
    (
        "valkey.asyncio.client",
        "Pipeline.immediate_execute_command",
        _ASYNC_COMMAND,
    ),
    ("valkey.asyncio.cluster", "ValkeyCluster.execute_command", _ASYNC_COMMAND),
    ("valkey.asyncio.cluster", "ClusterPipeline.execute", _ASYNC_PIPELINE),
)


class ValkeyInstrumentor(BaseInstrumentor):
    """Trace valkey-py commands by reusing the Redis instrumentor's span logic."""

    def instrumentation_dependencies(self) -> Collection[str]:
        """Instrument only when a supported valkey-py is installed."""
        return ("valkey >= 6.0.0",)

    def _instrument(self, **kwargs: Any) -> None:  # noqa: ANN401
        """Wrap the valkey client classes with the reused Redis span factories."""
        try:
            from opentelemetry.instrumentation.redis import (  # noqa: PLC0415
                _async_traced_execute_factory,
                _async_traced_execute_pipeline_factory,
                _traced_execute_factory,
                _traced_execute_pipeline_factory,
            )
        except ImportError:  # pragma: no cover
            _logger.warning(
                "Cannot instrument valkey: the span factories of "
                "opentelemetry-instrumentation-redis are unavailable. Pin a "
                "compatible release or open an issue against grelmicro."
            )
            return

        from opentelemetry.trace import get_tracer  # noqa: PLC0415
        from wrapt import wrap_function_wrapper  # noqa: PLC0415

        tracer = get_tracer(
            __name__, tracer_provider=kwargs.get("tracer_provider")
        )
        request_hook = kwargs.get("request_hook")
        response_hook = kwargs.get("response_hook")
        wrappers = {
            _SYNC_COMMAND: _traced_execute_factory(
                tracer, request_hook, response_hook
            ),
            _SYNC_PIPELINE: _traced_execute_pipeline_factory(
                tracer, request_hook, response_hook
            ),
            _ASYNC_COMMAND: _async_traced_execute_factory(
                tracer, request_hook, response_hook
            ),
            _ASYNC_PIPELINE: _async_traced_execute_pipeline_factory(
                tracer, request_hook, response_hook
            ),
        }
        for module, target, kind in _WRAP_TARGETS:
            wrap_function_wrapper(module, target, wrappers[kind])

    def _uninstrument(self, **kwargs: Any) -> None:  # noqa: ANN401, ARG002
        """Reverse `_instrument`, unwrapping every valkey client class."""
        import importlib  # noqa: PLC0415

        from opentelemetry.instrumentation.utils import unwrap  # noqa: PLC0415

        for module, target, _ in _WRAP_TARGETS:
            class_name, method = target.split(".")
            klass = getattr(importlib.import_module(module), class_name, None)
            if klass is not None:  # pragma: no branch
                unwrap(klass, method)
