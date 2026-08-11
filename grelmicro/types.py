"""Shared types used across grelmicro modules and in user configuration."""

from typing import Any, Generic, Literal

from pydantic import AnyUrl, GetCoreSchemaHandler, GetJsonSchemaHandler, Secret
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, PydanticCustomError, core_schema
from typing_extensions import TypeVar

from grelmicro._redact import redact_url
from grelmicro._timezone import normalize_timezone_name

__all__ = [
    "BackendScope",
    "Environment",
    "LogLevel",
    "SecretUrl",
    "TimeZoneName",
]

_TZ_ERROR_CODE = "time_zone_name"
"""Error code pydantic reports for an unusable timezone name."""

_TZ_ERROR_TEMPLATE = "{reason}"
"""Error template rendering the reason `normalize_timezone_name` reported."""

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
"""Standard logging level names, matching `logging.getLevelName` output."""

type Environment = Literal["development", "test", "staging", "production"]
"""Deployment tier the application runs in.

The well-known values of the OpenTelemetry `deployment.environment.name`
attribute. `Grelmicro(environment=...)` and `GREL_ENVIRONMENT` take one, and
`staging` and `production` turn the backend scope check into an error. A tier
your organisation calls something else maps onto the closest of the four.
"""

type BackendScope = Literal["process", "host", "cluster"]
"""How far a backend shares the state it holds.

Ordered: `process` is one process (Memory), `host` is the processes on one
host (SQLite), `cluster` is every process that connects to the backend
(Redis, Valkey, Postgres, Kubernetes). An Adapter declares its own as a
`scope` class attribute, and a Component `requires` at least one of them.
"""


class TimeZoneName(str):
    """An IANA timezone name, such as `UTC` or `Europe/Zurich`.

    Validation accepts any casing and stores the name in the casing the
    timezone database uses, so `europe/zurich` and `Europe/Zurich` behave
    the same on every filesystem.

    A name is accepted only when `zoneinfo` can load it. An abbreviation
    that names no zone, such as `PST`, is rejected where it is written
    rather than at the first use of the value.
    """

    __slots__ = ()

    @classmethod
    def _validate(cls, value: str) -> "TimeZoneName":
        """Return the validated timezone name.

        Raises:
            PydanticCustomError: If no timezone of that name can be loaded.
        """
        try:
            return cls(normalize_timezone_name(value))
        except ValueError as error:
            raise PydanticCustomError(
                _TZ_ERROR_CODE, _TZ_ERROR_TEMPLATE, {"reason": str(error)}
            ) from None

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: type[Any], handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        """Return the core schema that validates the timezone name."""
        return core_schema.no_info_after_validator_function(
            cls._validate,
            core_schema.str_schema(min_length=1),
        )


UrlType = TypeVar("UrlType", default=AnyUrl)
"""The URL type carried by a `SecretUrl`, defaulting to `AnyUrl`."""


class SecretUrl(Secret[UrlType], Generic[UrlType]):
    """A URL whose embedded credentials never reach any output.

    A URL carries its credentials inside itself: the password sits in the
    userinfo section (`redis://user:password@host`) and tokens often ride
    in the query string (`?api_key=...`). A plain URL field puts those in
    `repr()`, `model_dump()`, `model_dump_json()`, and any log line that
    prints the settings object.

    `SecretUrl` shows the URL with its credentials replaced by `***`
    everywhere the value is displayed or dumped, so an operator still
    reads the scheme, host, port, and path. The real URL comes back from
    `get_secret_value()`:

    ```python
    from pydantic import BaseModel, RedisDsn
    from grelmicro.types import SecretUrl


    class Settings(BaseModel):
        url: SecretUrl[RedisDsn]


    settings = Settings(url="redis://app:hunter2@cache:6379/0")

    print(repr(settings))
    #> Settings(url=SecretUrl('redis://app:***@cache:6379/0'))

    print(settings.url.get_secret_value())
    #> redis://app:hunter2@cache:6379/0
    ```

    Parametrize it with any pydantic URL type to keep that type's
    validation: `SecretUrl[RedisDsn]` accepts only Redis schemes, and
    `SecretUrl[PostgresDsn]` accepts the multi-host Postgres form.
    Unparametrized, `SecretUrl` accepts any URL. Use `SecretUrl[str]` for
    an endpoint that is not always a full URL, such as the `host:port`
    form the OTLP gRPC exporter takes.

    Parametrized with anything that is not a URL or a string, the whole
    value is masked instead, since nothing can be redacted from it
    safely.
    """

    def _display(self) -> str:
        """Return the URL with every credential replaced by `***`."""
        value = self.get_secret_value()
        if not isinstance(value, str) and not hasattr(value, "unicode_string"):
            return "**********"
        return redact_url(str(value), multi_host=hasattr(value, "hosts"))

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Mark the field write-only so generated schemas flag it as secret."""
        schema: dict[str, Any] = handler(core_schema)
        schema["writeOnly"] = True
        return schema
