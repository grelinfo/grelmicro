"""OpenTelemetry integration example."""

from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

from grelmicro.logging import configure_logging

# Set up OpenTelemetry
provider = TracerProvider()
processor = SimpleSpanProcessor(ConsoleSpanExporter())
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)

# Configure logging (auto-detects OpenTelemetry)
configure_logging()

# Get a tracer
tracer = trace.get_tracer(__name__)

# Logs inside spans will automatically include trace_id and span_id
with tracer.start_as_current_span("handle_request") as span:
    logger.info("Processing request", user_id=123, endpoint="/api/users")

    with tracer.start_as_current_span("database_query"):
        logger.info("Executing query", query="SELECT * FROM users")

    logger.info("Request completed", status="success")
