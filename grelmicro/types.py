"""Shared types used across grelmicro modules and in user configuration."""

from typing import Any, Generic, Literal

from pydantic import AnyUrl, GetJsonSchemaHandler, Secret
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema
from typing_extensions import TypeVar

from grelmicro._redact import redact_url

__all__ = ["LogLevel", "SecretUrl"]

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
"""Standard logging level names, matching `logging.getLevelName` output."""

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
