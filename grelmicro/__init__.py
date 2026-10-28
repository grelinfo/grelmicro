"""grelmicro is a lightweight framework/toolkit which is ideal for building async microservices in Python."""  # noqa: E501

import typing as _typing

if _typing.TYPE_CHECKING:
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
    from grelmicro._env_load_warning import (
        EnvLoadDefaultWarning as EnvLoadDefaultWarning,
    )
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
    "EnvLoadDefaultWarning",
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


_LAZY: dict[str, str] = {
    "AdapterNotRegisteredError": "grelmicro.errors",
    "AdmissionError": "grelmicro.errors",
    "AmbientBindingError": "grelmicro._app",
    "AmbientBindingWarning": "grelmicro.errors",
    "AmbiguousBackendError": "grelmicro._app",
    "AmbiguousProviderError": "grelmicro._app",
    "AppNotInstalledError": "grelmicro.errors",
    "BackendScopeError": "grelmicro.errors",
    "BackendScopeWarning": "grelmicro.errors",
    "Component": "grelmicro._component",
    "ComponentAlreadyRegisteredError": "grelmicro._app",
    "ComponentNotRegisteredError": "grelmicro._app",
    "DependencyNotFoundError": "grelmicro.errors",
    "DependencyVersionError": "grelmicro.errors",
    "EnvLoadOffWarning": "grelmicro.errors",
    "EventLoopDeadlockError": "grelmicro.errors",
    "ExternalConfig": "grelmicro.config",
    "Grelmicro": "grelmicro._app",
    "Grelmicro045Warning": "grelmicro.errors",
    "GrelmicroConfigWarning": "grelmicro.errors",
    "GrelmicroError": "grelmicro.errors",
    "GrelmicroFutureWarning": "grelmicro.errors",
    "GrelmicroMiddleware": "grelmicro._asgi",
    "HealthNotServedWarning": "grelmicro.errors",
    "LeaderNotRunningWarning": "grelmicro.errors",
    "LifecycleOrderError": "grelmicro._app",
    "MiddlewarePlacementError": "grelmicro.errors",
    "MiddlewarePlacementWarning": "grelmicro.errors",
    "MultipleActiveAppsError": "grelmicro.errors",
    "NoActiveAppError": "grelmicro._app",
    "OutOfContextError": "grelmicro.errors",
    "ProviderNotRegisteredError": "grelmicro.errors",
    "RoutesNotDeclaredWarning": "grelmicro.errors",
    "SentinelPasswordWarning": "grelmicro.errors",
    "SettingsValidationError": "grelmicro.errors",
    "UngatedCronWarning": "grelmicro.errors",
    "UnknownEnvironmentWarning": "grelmicro.errors",
    "Usable": "grelmicro._component",
}
"""The submodule each public name loads from."""

_SUBMODULES = frozenset({"config", "errors", "health", "providers", "types"})
"""The submodules `grelmicro.<name>` reaches without importing them first."""


def __getattr__(name: str) -> object:
    """Load `name` from its submodule, or warn on `EnvLoadDefaultWarning`."""
    module = _LAZY.get(name)
    if module is not None:
        import importlib  # noqa: PLC0415

        value = getattr(importlib.import_module(module), name)
        globals()[name] = value
        return value
    if name in _SUBMODULES:
        import importlib  # noqa: PLC0415

        return importlib.import_module(f"{__name__}.{name}")
    if name == "EnvLoadDefaultWarning":
        from grelmicro._env_load_warning import (  # noqa: PLC0415
            env_load_default_warning,
        )

        return env_load_default_warning()
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def __dir__() -> list[str]:
    """Return the loaded attributes and every public name not due for removal."""
    return sorted({*globals(), *_LAZY})
