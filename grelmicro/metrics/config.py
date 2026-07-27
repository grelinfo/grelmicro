"""Metrics Configuration."""

from base64 import b64encode
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    Field,
    PositiveFloat,
    SecretStr,
    model_validator,
)
from typing_extensions import Doc


class _CaseInsensitiveEnum(StrEnum):
    @classmethod
    def _missing_(cls, value: object) -> Self | None:
        if not isinstance(value, str):
            return None
        value = value.lower()
        for member in cls:
            if member.lower() == value:
                return member
        return None


class MetricsExporterType(_CaseInsensitiveEnum):
    """Metric exporter selection."""

    AUTO = "auto"
    OTLP_HTTP = "otlp-http"
    OTLP_GRPC = "otlp-grpc"
    PROMETHEUS = "prometheus"
    CONSOLE = "console"
    NONE = "none"


class MetricsConfig(BaseModel, frozen=True, extra="forbid"):
    """Metrics Config."""

    service_name: Annotated[
        str | None,
        Doc(
            "Service name resource attribute. Falls back to "
            "`OTEL_SERVICE_NAME` when unset."
        ),
    ] = None
    exporter: Annotated[
        MetricsExporterType,
        Doc(
            "Metric exporter. The default `auto` resolves to `otlp-http` when "
            "an endpoint is configured (the `endpoint` field, "
            "`OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`, or "
            "`OTEL_EXPORTER_OTLP_ENDPOINT`) and to `none` otherwise, so an "
            "unconfigured `Metrics()` exports nothing instead of falling back "
            "to `localhost:4318`."
        ),
    ] = MetricsExporterType.AUTO
    endpoint: Annotated[
        str | None,
        Doc(
            "Exporter endpoint. Falls back to `OTEL_EXPORTER_OTLP_ENDPOINT` "
            "when unset."
        ),
    ] = None
    headers: Annotated[
        dict[str, str],
        Doc(
            "Exporter headers. Falls back to `OTEL_EXPORTER_OTLP_HEADERS` "
            "when empty."
        ),
    ] = Field(default_factory=dict)
    basic_auth_username: Annotated[
        str | None,
        Doc(
            "HTTP Basic auth username for the OTLP exporter. Set together "
            "with `basic_auth_password` to send an `Authorization: Basic` "
            "header, built and attached on the exporter directly so it "
            "bypasses the fragile `OTEL_EXPORTER_OTLP_HEADERS` encoding."
        ),
    ] = None
    basic_auth_password: Annotated[
        SecretStr | None,
        Doc(
            "HTTP Basic auth password for the OTLP exporter. Set together "
            "with `basic_auth_username`."
        ),
    ] = None
    export_interval: Annotated[
        PositiveFloat,
        Doc(
            "Seconds between exports for the periodic reader. Applies to "
            "the OTLP and console exporters. The Prometheus exporter is "
            "pull-based and ignores this value."
        ),
    ] = 60.0
    export_timeout: Annotated[
        PositiveFloat,
        Doc(
            "Maximum seconds a single periodic export may take before it "
            "is abandoned."
        ),
    ] = 30.0
    resource_attributes: Annotated[
        dict[str, str],
        Doc("Extra resource attributes."),
    ] = Field(default_factory=dict)
    shutdown_timeout: Annotated[
        PositiveFloat,
        Doc(
            "Maximum seconds to wait for the `MeterProvider.shutdown()` "
            "flush. A slow or broken exporter no longer hangs application "
            "shutdown past this deadline."
        ),
    ] = 5.0

    @model_validator(mode="after")
    def _check_basic_auth(self) -> Self:
        """Require username and password together and guard header collisions."""
        has_username = self.basic_auth_username is not None
        has_password = self.basic_auth_password is not None
        if has_username != has_password:
            msg = (
                "basic_auth_username and basic_auth_password must be set "
                "together."
            )
            raise ValueError(msg)
        if has_username and any(
            key.lower() == "authorization" for key in self.headers
        ):
            msg = (
                "basic_auth conflicts with an Authorization header already "
                "set in headers. Use one or the other."
            )
            raise ValueError(msg)
        return self

    @property
    def authorization_header(self) -> str | None:
        """`Authorization: Basic` value from the credentials, or `None`.

        Encodes `username:password` as base64 per RFC 7617. Returns `None`
        when no Basic credentials are configured.
        """
        if self.basic_auth_username is None:
            return None
        password = (
            self.basic_auth_password.get_secret_value()
            if self.basic_auth_password
            else ""
        )
        raw = f"{self.basic_auth_username}:{password}"
        token = b64encode(raw.encode()).decode("ascii")
        return f"Basic {token}"
