"""grelmicro is a lightweight framework/toolkit which is ideal for building async microservices in Python."""  # noqa: E501

import typing as _typing

from grelmicro._app import (
    AmbientBindingError,
    AmbiguousBackendError,
    AmbiguousProviderError,
    ComponentAlreadyRegisteredError,
    ComponentNotRegisteredError,
    Grelmicro,
    LifecycleOrderError,
    NoActiveAppError,
)
from grelmicro._asgi import GrelmicroMiddleware
from grelmicro._component import Component, Usable
from grelmicro.config import ExternalConfig
from grelmicro.errors import (
    AdapterNotRegisteredError,
    AdmissionError,
    AmbientBindingWarning,
    AppNotInstalledError,
    BackendScopeError,
    BackendScopeWarning,
    DependencyNotFoundError,
    DependencyVersionError,
    EnvLoadOffWarning,
    EventLoopDeadlockError,
    Grelmicro045Warning,
    GrelmicroConfigWarning,
    GrelmicroError,
    GrelmicroFutureWarning,
    HealthNotServedWarning,
    LeaderNotRunningWarning,
    MiddlewarePlacementError,
    MiddlewarePlacementWarning,
    MultipleActiveAppsError,
    OutOfContextError,
    ProviderNotRegisteredError,
    RoutesNotDeclaredWarning,
    SentinelPasswordWarning,
    SettingsValidationError,
    UngatedCronWarning,
    UnknownEnvironmentWarning,
)

if _typing.TYPE_CHECKING:
    from grelmicro._env_load_warning import (
        EnvLoadDefaultWarning as EnvLoadDefaultWarning,
    )

__all__ = [
    "AdapterNotRegisteredError",
    "AdmissionError",
    "AmbientBindingError",
    "AmbientBindingWarning",
    "AmbiguousBackendError",
    "AmbiguousProviderError",
    "AppNotInstalledError",
    "BackendScopeError",
    "BackendScopeWarning",
    "Component",
    "ComponentAlreadyRegisteredError",
    "ComponentNotRegisteredError",
    "DependencyNotFoundError",
    "DependencyVersionError",
    "EnvLoadOffWarning",
    "EventLoopDeadlockError",
    "ExternalConfig",
    "Grelmicro",
    "Grelmicro045Warning",
    "GrelmicroConfigWarning",
    "GrelmicroError",
    "GrelmicroFutureWarning",
    "GrelmicroMiddleware",
    "HealthNotServedWarning",
    "LeaderNotRunningWarning",
    "LifecycleOrderError",
    "MiddlewarePlacementError",
    "MiddlewarePlacementWarning",
    "MultipleActiveAppsError",
    "NoActiveAppError",
    "OutOfContextError",
    "ProviderNotRegisteredError",
    "RoutesNotDeclaredWarning",
    "SentinelPasswordWarning",
    "SettingsValidationError",
    "UngatedCronWarning",
    "UnknownEnvironmentWarning",
    "Usable",
]


def __getattr__(name: str) -> object:
    """Return `EnvLoadDefaultWarning`, removed in 0.45.0, with a warning."""
    if name == "EnvLoadDefaultWarning":
        from grelmicro._env_load_warning import (  # noqa: PLC0415
            env_load_default_warning,
        )

        return env_load_default_warning()
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
