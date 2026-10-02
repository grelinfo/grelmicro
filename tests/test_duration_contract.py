"""Every float or timedelta field on a `*Config` is listed by what it is."""

import importlib
import pkgutil
import types
import typing
from datetime import timedelta

from pydantic import BaseModel

import grelmicro
from grelmicro._duration import Duration

WAITS = frozenset(
    {
        ("BulkheadConfig", "max_wait"),
        ("DiscoveryConfig", "retry_interval"),
        ("DiscoveryConfig", "timeout"),
        ("HealthChecksConfig", "timeout"),
        ("IdempotentRequestsConfig", "wait_timeout"),
        ("JWKSConfig", "retry_interval"),
        ("JWKSConfig", "timeout"),
        ("LeaderElectionConfig", "backend_timeout"),
        ("LeaderElectionConfig", "error_interval"),
        ("LeaderElectionConfig", "retry_interval"),
        ("LockConfig", "retry_interval"),
        ("MetricsConfig", "export_interval"),
        ("MetricsConfig", "export_timeout"),
        ("MetricsConfig", "shutdown_timeout"),
        ("OAuthClientConfig", "retry_interval"),
        ("OAuthClientConfig", "timeout"),
        ("OpsServerConfig", "request_timeout"),
        ("OpsServerConfig", "shutdown_timeout"),
        ("OutboxConfig", "poll_interval"),
        ("OutboxConfig", "retry_base"),
        ("OutboxConfig", "retry_max"),
        ("PostgresConfig", "command_timeout"),
        ("RateLimitedRequestsConfig", "max_wait"),
        ("ReadWriteLockConfig", "retry_interval"),
        ("RetryConfig", "max_seconds"),
        ("TasksConfig", "shutdown_timeout"),
        ("TimeoutConfig", "seconds"),
        ("TraceConfig", "shutdown_timeout"),
    }
)
"""Waits, timeouts passed to I/O and loop cadences, which stay float seconds."""

NOT_MOVED_YET = frozenset(
    {
        ("CachedResponsesConfig", "ttl"),
        ("ClientBansConfig", "duration"),
        ("ClientBansConfig", "window"),
        ("ConsecutiveCountConfig", "reset_timeout"),
        ("DiscoveryConfig", "cache_ttl"),
        ("DiscoveryConfig", "ttl"),
        ("DuplicateFilterConfig", "ttl"),
        ("HealthChecksConfig", "cache_ttl"),
        ("IdempotencyConfig", "ttl"),
        ("JWKSConfig", "cache_ttl"),
        ("JWKSConfig", "ttl"),
        ("JWTKeysConfig", "cache_ttl"),
        ("LeaderElectionConfig", "lease_duration"),
        ("LeaderElectionConfig", "renew_deadline"),
        ("LockConfig", "lease_duration"),
        ("OAuthClientConfig", "default_lifetime"),
        ("OAuthClientConfig", "refresh_before"),
        ("OutboxConfig", "keep_delivered"),
        ("OutboxConfig", "lease_duration"),
        ("ReadWriteLockConfig", "lease_duration"),
        ("TTLCacheConfig", "ttl"),
        ("TaskLockConfig", "lease_duration"),
        ("TaskLockConfig", "min_hold_duration"),
    }
)
"""Stored or enforced durations that do not take the shared duration type yet."""

NOT_DURATIONS = frozenset(
    {
        ("ApiShieldConfig", "max_rate"),
        ("InternalShieldConfig", "max_rate"),
        ("LeaderElectionConfig", "retry_jitter"),
        ("LockConfig", "retry_jitter"),
        ("OutboxConfig", "retry_jitter"),
        ("RateLimitFilterConfig", "cost"),
        ("RateLimitFilterConfig", "refill_rate"),
        ("ReadWriteLockConfig", "retry_jitter"),
        ("SlowShieldConfig", "max_rate"),
        ("TokenBucketConfig", "refill_rate"),
        ("TraceConfig", "sample_ratio"),
        ("_BaseShieldConfig", "max_rate"),
    }
)
"""Floats that are rates, ratios or fractions, not durations."""

_DURATION_CHECKS = typing.get_args(Duration)[1:]


def _import_all() -> None:
    for module in pkgutil.walk_packages(
        grelmicro.__path__, prefix="grelmicro."
    ):
        importlib.import_module(module.name)


def _subclasses(cls: type[BaseModel]) -> set[type[BaseModel]]:
    found = set()
    for sub in cls.__subclasses__():
        found.add(sub)
        found |= _subclasses(sub)
    return found


def _takes(annotation: object, kind: type) -> bool:
    if annotation is kind:
        return True
    if isinstance(annotation, types.UnionType) or typing.get_origin(
        annotation
    ) in (typing.Union, typing.Annotated):
        return any(_takes(arg, kind) for arg in typing.get_args(annotation))
    return False


def _unshared(config: type[BaseModel], name: str) -> bool:
    field = config.model_fields[name]
    if _takes(field.annotation, float):
        return True
    return _takes(field.annotation, timedelta) and not all(
        check in field.metadata for check in _DURATION_CHECKS
    )


def _listed_fields() -> set[tuple[str, str]]:
    _import_all()
    return {
        (config.__name__, name)
        for config in _subclasses(BaseModel)
        if config.__module__.startswith("grelmicro")
        and config.__name__.endswith("Config")
        for name in config.model_fields
        if _unshared(config, name)
    }


def test_every_float_or_timedelta_field_is_listed() -> None:
    """A float or timedelta field not on the shared type is listed."""
    assert _listed_fields() - WAITS - NOT_MOVED_YET - NOT_DURATIONS == set()


def test_every_listed_duration_still_exists() -> None:
    """Each listed field is still a float or timedelta on its `*Config`."""
    assert (WAITS | NOT_MOVED_YET | NOT_DURATIONS) - _listed_fields() == set()


def test_no_duration_is_both_a_wait_and_not_moved() -> None:
    """A field is in one list at most."""
    assert not WAITS & NOT_MOVED_YET
    assert not (WAITS | NOT_MOVED_YET) & NOT_DURATIONS
