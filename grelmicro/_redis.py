"""Internal shared Redis URL resolution and client factory.

Used by both the sync (RedisSyncAdapter) and cache (RedisCacheAdapter)
domains to avoid duplicating environment-variable handling. Not part of
the public API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import RedisDsn, ValidationError
from pydantic_core import Url
from pydantic_settings import BaseSettings
from redis.asyncio.client import Redis

if TYPE_CHECKING:
    from grelmicro.errors import SettingsValidationError


class _RedisSettings(BaseSettings):
    """Redis settings from the environment variables.

    Shared across sync and cache domains. Any domain-specific
    extension should subclass rather than modify this class.
    """

    REDIS_HOST: str | None = None
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None
    REDIS_URL: RedisDsn | None = None


def _get_redis_url(
    error_class: type[SettingsValidationError],
) -> str:
    """Get the Redis URL from the environment variables.

    Args:
        error_class: The domain-specific error class to raise on validation failure.

    Raises:
        error_class: If the environment variables are invalid, both set, or neither set.
    """
    try:
        settings = _RedisSettings()
    except ValidationError as error:
        raise error_class(error) from None

    if settings.REDIS_URL and not settings.REDIS_HOST:
        return settings.REDIS_URL.unicode_string()

    if settings.REDIS_HOST and not settings.REDIS_URL:
        return Url.build(
            scheme="redis",
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            path=str(settings.REDIS_DB),
            password=settings.REDIS_PASSWORD,
        ).unicode_string()

    if settings.REDIS_URL and settings.REDIS_HOST:
        msg = "Set either REDIS_URL or REDIS_HOST, not both"
    else:
        msg = "Either REDIS_URL or REDIS_HOST must be set"
    raise error_class(msg)


def _create_redis_client(
    url: RedisDsn | str | None,
    error_class: type[SettingsValidationError],
) -> tuple[str, Redis[bytes]]:
    """Resolve the Redis URL and create an async Redis client.

    Each call creates a new ``Redis`` instance with its own connection pool.
    See ``docs/architecture/backends.md`` for the rationale.

    Args:
        url: Explicit Redis URL, or None to resolve from environment variables.
        error_class: The domain-specific error class to raise on validation failure.

    Returns:
        A tuple of (resolved_url, redis_client).

    Raises:
        error_class: If the URL cannot be resolved from environment variables.
    """
    resolved_url = str(url) if url else _get_redis_url(error_class)
    return resolved_url, Redis.from_url(resolved_url)
