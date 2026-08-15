"""Errors."""

from typing import ClassVar

from pydantic import ValidationError


class GrelmicroError(Exception):
    """Base grelmicro error."""


class GrelmicroConfigWarning(UserWarning):
    """Warned when configuration is set in a way that will not take effect.

    A category of its own so it can be filtered precisely, without silencing
    every `UserWarning` and without matching on the message text:

    ```toml
    filterwarnings = ["error", "ignore::grelmicro.GrelmicroConfigWarning"]
    ```

    Each diagnostic also has its own subclass, so one can be silenced without
    silencing the rest. The `code` attribute is the diagnostic's stable
    identifier, matching the section at `/diagnostics/#{code}`.
    """

    code: ClassVar[str] = ""
    """Stable diagnostic code, empty on the base category."""


class EnvLoadOffWarning(GrelmicroConfigWarning):
    """A `GREL_*` variable is set but `GREL_ENV_LOAD` is off."""

    code: ClassVar[str] = "env-load-off"


class UnknownEnvironmentWarning(GrelmicroConfigWarning):
    """`GREL_ENVIRONMENT` names no tier grelmicro knows."""

    code: ClassVar[str] = "unknown-environment"


class BackendScopeWarning(GrelmicroConfigWarning):
    """A bound backend reaches less far than its component requires.

    The same problem raises `BackendScopeError` in `staging` and
    `production`. Both carry the `backend-scope` code.
    """

    code: ClassVar[str] = "backend-scope"


class AmbientBindingWarning(GrelmicroConfigWarning):
    """Ambient components are registered but the binding middleware is missing.

    The same problem raises `AmbientBindingError` under
    `Grelmicro(strict=True)`. Both carry the `ambient-binding` code.
    """

    code: ClassVar[str] = "ambient-binding"


class SentinelPasswordWarning(GrelmicroConfigWarning):
    """A Sentinel password is set but the URL scheme cannot apply it."""

    code: ClassVar[str] = "sentinel-password"


class BackendScopeError(GrelmicroError, RuntimeError):
    """Raised when a backend does not reach as far as its component requires.

    A `Lock` on a memory backend excludes nothing once a second replica runs.
    So in `staging` and `production`, a component bound to a backend whose
    `scope` falls short of what it requires refuses to open, before the first
    connection is made. Wire a backend that reaches far enough, or pass
    `requires=` to declare the reach you meant.

    `micro.check_backends()` raises the same error from a test, so the wiring
    is answered for before a pod answers for it.

    Carries the `backend-scope` code, the same one `BackendScopeWarning`
    carries when no tier is declared.
    """

    code: ClassVar[str] = "backend-scope"


class AdmissionError(GrelmicroError):
    """Raised when a gatekeeping primitive refuses a call.

    The shared base for every "turned away" rejection: a rate limiter over
    budget (`RateLimitExceededError`), a full bulkhead (`BulkheadFullError`),
    an open circuit breaker (`CircuitBreakerError`), or a non-blocking lock
    acquire that would block (`WouldBlockError`). Catch `AdmissionError` to
    handle any admission rejection with one `except`.
    """


class WouldBlockError(AdmissionError, RuntimeError):
    """Raised by a non-blocking acquire that would have blocked."""


class OutOfContextError(GrelmicroError, RuntimeError):
    """Outside Context Error.

    Raised when a method is called outside of the context manager.
    """

    def __init__(self, cls: object, method_name: str | None = None) -> None:
        """Initialize the error.

        Pass a context object and a method name for the default message,
        or a single ready-made message string.
        """
        if method_name is None:
            super().__init__(str(cls))
        else:
            super().__init__(
                f"Could not call {cls.__class__.__name__}.{method_name} "
                "outside of the context manager"
            )


class DependencyNotFoundError(GrelmicroError, ImportError):
    """Dependency Not Found Error."""

    def __init__(self, *, module: str) -> None:
        """Initialize the error."""
        super().__init__(
            f"Could not import module {module}, try running 'pip install {module}'"
        )


class MultipleActiveAppsError(GrelmicroError, RuntimeError):
    """Raised when a second `Grelmicro` app is opened while one is active.

    Components such as `Log` and `Trace` configure process-global state
    (the stdlib root logger, the OpenTelemetry tracer provider) and restore
    it in reverse order on exit. Two overlapping app lifecycles in the same
    process would restore that state out of order and clobber each other,
    so a second concurrent app is blocked by default. Run apps one at a
    time, or pass `Grelmicro(allow_multiple=True)` if you are sure no two
    active apps configure the same global state.
    """

    def __init__(self) -> None:
        """Initialize the error."""
        super().__init__(
            "Another Grelmicro app is already active in this process. "
            "Components like Log and Trace own process-global state that "
            "cannot be shared across overlapping app lifecycles. Open apps "
            "one at a time, or pass Grelmicro(allow_multiple=True) to opt "
            "out of this check."
        )


class ProviderNotRegisteredError(GrelmicroError, LookupError):
    """Raised when no Provider is registered under a requested short name.

    Short names resolve against the `grelmicro.providers` entry-point group.
    A miss usually means the package that ships the Provider is not installed,
    or the name is misspelled.
    """

    def __init__(self, short_name: str, available: list[str]) -> None:
        """Initialize the error."""
        known = ", ".join(available) if available else "none installed"
        super().__init__(
            f"No provider registered as {short_name!r} in the "
            f"'grelmicro.providers' entry-point group. Available: {known}. "
            f"Install the package that ships it, or check the name."
        )


class AdapterNotRegisteredError(GrelmicroError, LookupError):
    """Raised when no Adapter is registered under a short name for a kind.

    Short names resolve against the `grelmicro.{kind}.adapters` entry-point
    group. A miss usually means the package that ships the Adapter is not
    installed, or the name is misspelled.
    """

    def __init__(
        self, kind: str, short_name: str, available: list[str]
    ) -> None:
        """Initialize the error."""
        group = f"grelmicro.{kind}.adapters"
        known = ", ".join(available) if available else "none installed"
        super().__init__(
            f"No {kind} adapter registered as {short_name!r} in the "
            f"{group!r} entry-point group. Available: {known}. "
            f"Install the package that ships it, or check the name."
        )


class SettingsValidationError(GrelmicroError, ValueError):
    """Settings Validation Error.

    Pydantic ValidationError messages already describe the failure shape
    ("Input should be a valid string", "Input should be greater than 0",
    ...) so the raw input is intentionally omitted from the rendered
    error. Settings often originate from environment variables that may
    carry credentials (DSNs, tokens), and echoing the offending value
    into a log line would leak them.
    """

    def __init__(self, error: ValidationError | str) -> None:
        """Initialize the error."""
        if isinstance(error, ValidationError):
            details = "\n".join(
                f"- {data['loc'][0]}: {data['msg']}" for data in error.errors()
            )
        else:
            details = error

        super().__init__(f"Could not validate settings:\n{details}")
