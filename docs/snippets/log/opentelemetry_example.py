"""OpenTelemetry integration example."""

from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from grelmicro.log import LogBackendType, LogFormatType, configure

# Set up OpenTelemetry
trace.set_tracer_provider(TracerProvider())

# Configure logging (auto-detects OpenTelemetry)
configure(backend=LogBackendType.LOGURU, format=LogFormatType.JSON)

# Get a tracer
tracer = trace.get_tracer(__name__)

# Logs inside spans will automatically include trace_id and span_id
with tracer.start_as_current_span("handle_request"):
    logger.info("Processing request", user_id=123, endpoint="/api/users")

    with tracer.start_as_current_span("database_query"):
        logger.info("Executing query", query="SELECT * FROM users")

    logger.info("Request completed", status="success")
