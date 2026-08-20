"""Errors."""

import re
from typing import Any, ClassVar, cast, get_args

from pydantic import ValidationError
from pydantic_core import ErrorType

from grelmicro._guards import is_instance


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
    an open circuit breaker (`CircuitBreakerError`), or a lock acquire that
    did not get in (`WouldBlockError`, and `LockTimeoutError` for the
    bounded form). Catch `AdmissionError` to handle any admission
    rejection with one `except`.
    """


class WouldBlockError(AdmissionError, RuntimeError):
    """Raised by a non-blocking acquire that would have blocked.

    Catch this to handle a lock you did not get, whether the acquire
    refused to wait at all or waited and ran out. `LockTimeoutError`
    subclasses it, because the outcome is the same and only the wait
    differs.
    """


class LockTimeoutError(WouldBlockError, TimeoutError):
    """Raised by a bounded acquire whose `timeout` elapsed.

    A `WouldBlockError`, because the outcome is the one a non-blocking
    acquire reports: another holder has the lock. A builtin
    `TimeoutError` as well, so an `except TimeoutError` around a bounded
    acquire keeps working.

    Carries the lock and the wait that elapsed, which a bare
    `TimeoutError` could not, and which is why a bounded acquire used to
    be indistinguishable from a socket timeout raised underneath it.
    """

    def __init__(
        self,
        *,
        name: str,
        timeout: float,
    ) -> None:
        """Initialize the error."""
        self.name = name
        self.timeout = timeout
        super().__init__(f"Lock '{name}' not acquired within {timeout}s")


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
    """Raised when a configuration value fails validation.

    Every grelmicro class raises this one error, whichever pattern or
    component it is, so one `except` covers the whole library. A config
    class you build yourself, such as `RetryConfig(...)`, raises
    pydantic's `ValidationError` like any pydantic model.

    Subclasses `ValueError`, which `pydantic.ValidationError` also is, so
    an `except ValueError` catches either.

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
            # `loc` is empty for a model-level validator, which checks the
            # config as a whole rather than one field. Indexing it directly
            # raised `IndexError` and hid the real error.
            details = "\n".join(
                f"- {'.'.join(str(part) for part in data['loc']) or '(config)'}"
                f": {_scrub(data['msg'], data.get('input'), data['type'])}"
                for data in error.errors(include_url=False)
            )
        else:
            details = error

        super().__init__(f"Could not validate settings:\n{details}")


_REDACTED = "[redacted]"
"""Stands in for a rejected value that a message would otherwise carry."""

_IMPORT_ERROR_MESSAGE = "Invalid python path"
"""Replaces pydantic's `import_error` text, which quotes the module.

The module is one half of the rejected value, so the message is replaced
outright rather than edited. What remains still says which field failed and
why, and `docs/architecture/config.md` states the form an entry must take.
"""

_ECHOING_ERROR_TYPES = frozenset(
    {"union_tag_invalid", "value_error", "assertion_error"}
)
"""Pydantic error types whose `msg` can repeat the rejected input verbatim.

Checked against pydantic rather than assumed. `union_tag_invalid` quotes the
tag, while the constraint messages (`int_parsing`, `literal_error`,
`greater_than`, ...) describe what was expected and never repeat what
arrived. `value_error` and `assertion_error` carry a message someone else
wrote, so they are included in case a third-party config class names the
value.

Scrubbing every type would garble the messages that are already safe:
with `INF` as the input, "Input should be 'DEBUG', 'INFO', ..." would lose
the option the operator was reaching for.
"""


_KNOWN_ERROR_TYPES = frozenset(get_args(ErrorType))
"""Every error code pydantic raises itself.

Read from pydantic rather than listed here, so a code added by a later
release is not mistaken for someone's own. An empty set means the
catalogue could not be read, and `_is_echoing` then falls back to the
explicit list alone rather than treating every message as unreviewed.
"""

_SAFE_CUSTOM_ERROR_TYPES = frozenset({"time_zone_name"})
"""grelmicro's own custom codes, whose messages are written not to echo.

`time_zone_name` offers a member of the timezone database, which can
share a segment with the name it rejects. Scrubbing would take the
suggestion apart, and the message carries nothing else.
"""


def _is_echoing(error_type: str) -> bool:
    """Whether a message of this type can repeat the rejected input.

    A code pydantic does not define comes from a `PydanticCustomError`
    someone wrote, so its message is unreviewed and treated as echoing.
    Without this the backstop covered only the codes pydantic ships and
    missed every custom one.
    """
    if error_type in _ECHOING_ERROR_TYPES:
        return True
    if error_type in _SAFE_CUSTOM_ERROR_TYPES:
        return False
    return bool(_KNOWN_ERROR_TYPES) and error_type not in _KNOWN_ERROR_TYPES


def _text_of(value: object) -> str:
    """Render a rejected value, and never raise doing it.

    A `str` subclass runs caller code from `__str__`, and this is the
    backstop that keeps a credential out of an error message, so it
    cannot be the thing that fails. An unrenderable value is one no
    message could have quoted.
    """
    try:
        return str(value)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return ""


def _candidates(value: object) -> set[str]:
    """Return every string inside `value`, whole and unsplit.

    Containers are walked because pydantic reports the input at the level
    that failed, not the level that offends: `union_tag_invalid` hands back
    the whole mapping, with the tag one key down.

    Numbers count too. A validator that names a rejected number leaks a
    value read from the environment the same way a string does, and the
    reason to withhold it does not depend on its type.

    The strings are never split on separators. Splitting once seemed
    thorough and was the bug: a piece of the rejected value matches the
    correct value that shares it, so `Europe/Zurichh` redacted the `Europe`
    of the `did you mean 'Europe/Zurich'` written to replace it.

    Every shape test is total. This runs while an error is already being
    reported, and a lazy proxy raising from `__class__` used to replace
    the `SettingsValidationError` a component owes its caller.
    """
    if is_instance(value, bool):
        # `True` and `False` carry nothing, and removing those words would
        # garble a message that legitimately uses them.
        return set()
    if is_instance(value, (str, int, float)):
        text = _text_of(value)
        return {text} if len(text) >= _MIN_REDACTED_LENGTH else set()
    if is_instance(value, dict):
        found: set[str] = set()
        for item in cast("dict[Any, Any]", value).values():
            found |= _candidates(item)
        return found
    if is_instance(value, (list, tuple, set)):
        found = set()
        for item in cast("tuple[Any, ...]", value):
            found |= _candidates(item)
        return found
    return set()


def _scrub(msg: str, value: object, error_type: str) -> str:
    """Remove the rejected input from a message built elsewhere.

    grelmicro's own validators are written not to name the value. This is
    the backstop that holds rule R7 when the message came from pydantic or
    from a third-party config class.

    Only whole-token occurrences are removed. A typo is usually a prefix of
    the value that was meant, so a substring replace would take the correct
    spelling out of the very message offering it.
    """
    if error_type == "import_error":
        return _IMPORT_ERROR_MESSAGE
    if not _is_echoing(error_type):
        return msg
    for candidate in sorted(_candidates(value), key=len, reverse=True):
        msg = re.sub(
            rf"(?<![\w]){re.escape(candidate)}(?![\w])", _REDACTED, msg
        )
    return msg


_MIN_REDACTED_LENGTH = 3
"""Shortest input worth removing.

Below this the value is as likely to be a word in the surrounding sentence
as it is to be the rejected input, and removing it would garble the message
without protecting anything.
"""
