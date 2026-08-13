"""URL validation shared by the providers that take a URL."""

from __future__ import annotations

from functools import lru_cache
from types import NoneType
from typing import TYPE_CHECKING, Any, get_args

from pydantic import ConfigDict, create_model

if TYPE_CHECKING:
    from pydantic import BaseModel
    from pydantic_settings import BaseSettings


@lru_cache
def _url_model(settings_cls: type[BaseSettings]) -> type[BaseModel]:
    """Build a one-field model that validates a URL as `settings_cls` does.

    The field type is read from the `url` field of the environment
    settings, so every provider has exactly one URL type. The field is
    optional there and required here, since a URL reaches this model only
    once a caller has passed one.
    """
    annotation = settings_cls.model_fields["url"].annotation
    arguments = get_args(annotation)
    url_type: Any = (
        next(argument for argument in arguments if argument is not NoneType)
        if NoneType in arguments
        else annotation
    )
    name = settings_cls.__name__.removeprefix("_").removesuffix("EnvSettings")
    return create_model(
        f"{name}Url",
        __config__=ConfigDict(hide_input_in_errors=True),
        url=(url_type, ...),
    )


def validate_url(url: str, *, settings_cls: type[BaseSettings]) -> str:
    """Validate a URL against the type the environment path uses.

    Returns the URL as pydantic writes it back: the scheme lowercased,
    everything else as it was given.

    Raises:
        ValidationError: If the URL is malformed, or carries a scheme the
            provider does not understand.
    """
    validated = _url_model(settings_cls).model_validate({"url": url})
    return str(validated.model_dump()["url"].get_secret_value())
