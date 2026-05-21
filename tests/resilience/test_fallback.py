"""Fallback policy tests."""

import asyncio as _asyncio

import pytest
from pydantic import ValidationError

from grelmicro.resilience import (
    Fallback,
    FallbackConfig,
    FallbackResult,
    Match,
    Outcome,
    fallback,
    falling_back,
)

_FORTYTWO = 42
_SEVEN = 7
_NINETY_NINE = 99


# --- FallbackConfig validation --------------------------------------------


def test_config_requires_when() -> None:
    """`FallbackConfig` raises when `when` is missing."""
    with pytest.raises(ValidationError):
        FallbackConfig(default=1)  # type: ignore[call-arg]  # ty: ignore[missing-argument]


def test_config_requires_default_or_factory() -> None:
    """`FallbackConfig` raises when neither default nor factory is set."""
    with pytest.raises(ValidationError):
        FallbackConfig(when=Match.exception(ValueError))


def test_config_rejects_both_default_and_factory() -> None:
    """`FallbackConfig` raises when both default and factory are set."""
    with pytest.raises(ValidationError):
        FallbackConfig(
            when=Match.exception(ValueError),
            default=1,
            factory=lambda _exc: 2,
        )


def test_config_accepts_default_none() -> None:
    """`None` is a valid `default` value."""
    cfg = FallbackConfig(when=Match.exception(ValueError), default=None)
    assert cfg.default is None


def test_config_frozen() -> None:
    """`FallbackConfig` is frozen."""
    cfg = FallbackConfig(when=Match.exception(ValueError), default=1)
    with pytest.raises(ValidationError):
        cfg.default = 2  # type: ignore[misc]  # ty: ignore[invalid-assignment]


# --- Class-form construction ----------------------------------------------


def test_fallback_constructs_with_class_filter() -> None:
    """Construct accepts a single class filter."""
    policy = Fallback("test", when=ValueError, default=[])
    assert policy.name == "test"
    assert policy.config.default == []


def test_fallback_normalizes_single_class_to_match() -> None:
    """Single exception class is coerced to `Match.exception(...)`."""
    policy = Fallback("test", when=ValueError, default=0)
    matcher = policy.config.when
    assert matcher(Outcome.from_exception(ValueError()))
    assert not matcher(Outcome.from_exception(KeyError()))


def test_fallback_accepts_tuple_filter() -> None:
    """Tuple of classes is coerced to a multi-class match."""
    policy = Fallback("test", when=(ValueError, KeyError), default=None)
    matcher = policy.config.when
    assert matcher(Outcome.from_exception(ValueError()))
    assert matcher(Outcome.from_exception(KeyError()))
    assert not matcher(Outcome.from_exception(TypeError()))


def test_fallback_rejects_both_default_and_factory() -> None:
    """Class form enforces mutual exclusion too."""
    with pytest.raises(ValidationError):
        Fallback("test", when=ValueError, default=1, factory=lambda _e: 2)


def test_fallback_accepts_callable_filter() -> None:
    """Callable predicate becomes a `Match.exception(predicate)`."""
    policy = Fallback(
        "test",
        when=lambda exc: isinstance(exc, ValueError) and "fb" in str(exc),
        default="fb",
    )
    matcher = policy.config.when
    assert matcher(Outcome.from_exception(ValueError("fb me")))
    assert not matcher(Outcome.from_exception(ValueError("nope")))


# --- Decorator behavior ---------------------------------------------------


async def test_decorator_returns_function_value_on_success() -> None:
    """No fallback fires on success."""

    @fallback(when=ValueError, default="fb")
    async def fn() -> str:
        return "ok"

    assert await fn() == "ok"


async def test_decorator_returns_default_on_matched_exception() -> None:
    """Matched exception swaps in the default."""

    @fallback(when=ValueError, default="fb")
    async def fn() -> str:
        msg = "boom"
        raise ValueError(msg)

    assert await fn() == "fb"


async def test_decorator_calls_factory_with_exception() -> None:
    """Factory receives the exception and produces the fallback."""
    seen: list[BaseException] = []

    def make(exc: BaseException) -> str:
        seen.append(exc)
        return f"F:{exc}"

    @fallback(when=ValueError, factory=make)
    async def fn() -> str:
        msg = "boom"
        raise ValueError(msg)

    assert await fn() == "F:boom"
    assert len(seen) == 1
    assert isinstance(seen[0], ValueError)


async def test_decorator_reraises_unmatched_exception() -> None:
    """Unmatched exceptions escape immediately."""

    @fallback(when=ValueError, default="fb")
    async def fn() -> None:
        msg = "nope"
        raise KeyError(msg)

    with pytest.raises(KeyError, match="nope"):
        await fn()


def test_decorator_on_sync_function() -> None:
    """Decorator auto-detects sync functions."""
    calls: list[int] = []

    @fallback(when=ValueError, default="fb")
    def fn() -> str:
        calls.append(1)
        if len(calls) == 1:
            return "ok"
        msg = "boom"
        raise ValueError(msg)

    assert fn() == "ok"
    assert fn() == "fb"


def test_decorator_sync_reraises_unmatched() -> None:
    """Sync wrapper raises immediately when the matcher rejects."""

    @fallback(when=ValueError, default="fb")
    def fn() -> None:
        msg = "nope"
        raise KeyError(msg)

    with pytest.raises(KeyError, match="nope"):
        fn()


async def test_decorator_does_not_catch_cancelled_error() -> None:
    """`asyncio.CancelledError` propagates regardless of `when`."""

    @fallback(when=Exception, default="fb")
    async def fn() -> None:
        raise _asyncio.CancelledError

    with pytest.raises(_asyncio.CancelledError):
        await fn()


# --- Class-form decorator -------------------------------------------------


async def test_class_form_as_decorator() -> None:
    """A `Fallback` instance can be called as a decorator."""
    policy = Fallback("test", when=ValueError, default="fb")

    @policy
    async def fn() -> str:
        msg = "boom"
        raise ValueError(msg)

    assert await fn() == "fb"


def test_class_form_decorator_on_sync_function() -> None:
    """A `Fallback` instance can decorate a sync function."""
    policy = Fallback("test", when=ValueError, default="fb")

    @policy
    def fn() -> str:
        msg = "boom"
        raise ValueError(msg)

    assert fn() == "fb"


# --- Block form -----------------------------------------------------------


async def test_block_form_success_path() -> None:
    """`async with falling_back(...)` returns the value set inside."""
    async with falling_back(when=ValueError, default=_NINETY_NINE) as result:
        result.set(_SEVEN)
    assert result.value == _SEVEN


async def test_block_form_falls_back_on_matched_exception() -> None:
    """A matched exception is suppressed and the default replaces it."""
    async with falling_back(when=ValueError, default=_NINETY_NINE) as result:
        msg = "boom"
        raise ValueError(msg)
    assert result.value == _NINETY_NINE


async def test_block_form_factory_receives_exception() -> None:
    """Factory receives the suppressed exception."""
    async with falling_back(
        when=ValueError, factory=lambda exc: f"F:{exc}"
    ) as result:
        msg = "boom"
        raise ValueError(msg)
    assert result.value == "F:boom"


async def test_block_form_reraises_unmatched() -> None:
    """Unmatched exceptions propagate."""
    with pytest.raises(KeyError, match="nope"):  # noqa: PT012
        async with falling_back(when=ValueError, default=_NINETY_NINE):
            msg = "nope"
            raise KeyError(msg)


def test_block_form_sync() -> None:
    """The block form also works as a sync context manager."""
    with falling_back(when=ValueError, default=_FORTYTWO) as result:
        msg = "boom"
        raise ValueError(msg)
    assert result.value == _FORTYTWO


def test_fallback_result_value_before_set_raises() -> None:
    """Accessing `.value` before any set raises."""
    result: FallbackResult[int] = FallbackResult()
    with pytest.raises(RuntimeError, match="before any value was set"):
        _ = result.value


async def test_block_form_propagates_cancellederror() -> None:
    """`CancelledError` propagates from a block with broad `when=`."""
    with pytest.raises(_asyncio.CancelledError):
        async with falling_back(when=Exception, default=0):
            raise _asyncio.CancelledError


# --- Reconfigure -----------------------------------------------------------


async def test_reconfigure_changes_default() -> None:
    """Reconfigure publishes the new config to future calls."""
    policy = Fallback("test", when=ValueError, default=1)
    new = policy.config.model_copy(update={"default": _FORTYTWO})
    await policy.reconfigure(new)

    @policy
    async def fn() -> int:
        msg = "boom"
        raise ValueError(msg)

    assert await fn() == _FORTYTWO


# --- Env-driven configuration ---------------------------------------------


async def test_env_populates_when_and_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GREL_FALLBACK_{NAME}_WHEN` and `_DEFAULT` populate unset fields."""
    monkeypatch.setenv("GREL_FALLBACK_RECS_WHEN", "builtins.ValueError")
    monkeypatch.setenv("GREL_FALLBACK_RECS_DEFAULT", "[]")
    policy = Fallback("recs")  # type: ignore[call-arg]
    assert policy.config.default == []
    assert policy.config.when(Outcome.from_exception(ValueError()))


async def test_kwargs_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller kwargs win over env."""
    monkeypatch.setenv("GREL_FALLBACK_BAR_DEFAULT", "1")
    policy = Fallback("bar", when=ValueError, default=_SEVEN)
    assert policy.config.default == _SEVEN


async def test_from_config_bypasses_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Fallback.from_config()` ignores env even when set."""
    monkeypatch.setenv("GREL_FALLBACK_BAZ_DEFAULT", "9")
    cfg = FallbackConfig(when=Match.exception(ValueError), default=1)
    policy = Fallback.from_config("baz", cfg)
    assert policy.config.default == 1


async def test_env_when_rejects_non_dotted_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare-name env entry raises a clear error."""
    monkeypatch.setenv("GREL_FALLBACK_BAD_WHEN", "ValueError")
    monkeypatch.setenv("GREL_FALLBACK_BAD_DEFAULT", "0")
    with pytest.raises((ValidationError, ValueError)):
        Fallback("bad")  # type: ignore[call-arg]


async def test_env_default_parses_null_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GREL_FALLBACK_{NAME}_DEFAULT=null` becomes `None`."""
    monkeypatch.setenv("GREL_FALLBACK_NULLD_WHEN", "builtins.ValueError")
    monkeypatch.setenv("GREL_FALLBACK_NULLD_DEFAULT", "null")
    policy = Fallback("nulld")  # type: ignore[call-arg]
    assert policy.config.default is None


async def test_env_default_keeps_non_json_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-JSON env string is kept as a plain string."""
    monkeypatch.setenv("GREL_FALLBACK_RAW_WHEN", "builtins.ValueError")
    monkeypatch.setenv("GREL_FALLBACK_RAW_DEFAULT", "hello world")
    policy = Fallback("raw")  # type: ignore[call-arg]
    assert policy.config.default == "hello world"


def test_string_default_kwarg_is_not_json_parsed() -> None:
    """A string `default=` passed in code stays a string, never JSON-parsed.

    Regression test: an earlier `field_validator("default")` ran on every
    string and silently turned `default="[1,2,3]"` into a list. The
    coercion is env-only now.
    """
    policy = Fallback("kwarg", when=ValueError, default="[1,2,3]")
    assert policy.config.default == "[1,2,3]"


def test_decorator_string_default_kwarg_is_not_json_parsed() -> None:
    """Same regression check for the `@fallback(...)` decorator."""

    @fallback(when=ValueError, default="null")
    def fn() -> str:
        msg = "boom"
        raise ValueError(msg)

    assert fn() == "null"


def test_config_string_default_is_not_json_parsed() -> None:
    """`FallbackConfig(default="[]")` keeps the string verbatim."""
    cfg = FallbackConfig(when=Match.exception(ValueError), default="[]")
    assert cfg.default == "[]"


# --- Invalid `when=` ------------------------------------------------------


def test_when_rejects_invalid_value() -> None:
    """Non-Match, non-class, non-tuple, non-callable raises."""
    with pytest.raises((ValidationError, TypeError)):
        Fallback("api", when=42, default=0)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


# --- Env: bad `when=` FQNs ------------------------------------------------


async def test_env_when_rejects_unknown_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FQN that points to a missing module raises with a clear message."""
    monkeypatch.setenv("GREL_FALLBACK_BAD2_WHEN", "no_such_module.NoClass")
    monkeypatch.setenv("GREL_FALLBACK_BAD2_DEFAULT", "0")
    with pytest.raises(
        (ValidationError, ValueError), match="cannot import module"
    ):
        Fallback("bad2")  # type: ignore[call-arg]


async def test_env_when_rejects_unknown_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FQN that points to a missing attribute raises with a clear message."""
    monkeypatch.setenv("GREL_FALLBACK_BAD3_WHEN", "builtins.NoSuchClass")
    monkeypatch.setenv("GREL_FALLBACK_BAD3_DEFAULT", "0")
    with pytest.raises((ValidationError, ValueError), match="has no attribute"):
        Fallback("bad3")  # type: ignore[call-arg]


async def test_env_when_rejects_non_exception_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FQN that resolves to a non-Exception class raises."""
    monkeypatch.setenv("GREL_FALLBACK_BAD4_WHEN", "builtins.int")
    monkeypatch.setenv("GREL_FALLBACK_BAD4_DEFAULT", "0")
    with pytest.raises((ValidationError, TypeError)):
        Fallback("bad4")  # type: ignore[call-arg]


# --- Env opt-out and factory-only paths -----------------------------------


def test_env_load_false_skips_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`env_load=False` ignores env vars even when set."""
    monkeypatch.setenv("GREL_FALLBACK_NOENV_DEFAULT", "9")
    policy = Fallback("noenv", when=ValueError, default=_SEVEN, env_load=False)
    assert policy.config.default == _SEVEN


def test_env_load_with_factory_kwarg_and_no_env_default() -> None:
    """`env_load=True` + factory kwarg + no env DEFAULT is a valid path."""
    policy = Fallback(
        "factenv",
        when=ValueError,
        factory=lambda _exc: "fb",
        env_load=True,
    )
    assert policy.config.factory is not None


# --- Class-form decorator: unmatched re-raise -----------------------------


async def test_class_form_decorator_reraises_unmatched_async() -> None:
    """Class-form async wrapper re-raises unmatched exceptions."""
    policy = Fallback("rr", when=ValueError, default="fb")

    @policy
    async def fn() -> None:
        msg = "nope"
        raise KeyError(msg)

    with pytest.raises(KeyError, match="nope"):
        await fn()


def test_class_form_decorator_reraises_unmatched_sync() -> None:
    """Class-form sync wrapper re-raises unmatched exceptions."""
    policy = Fallback("rr2", when=ValueError, default="fb")

    @policy
    def fn() -> None:
        msg = "nope"
        raise KeyError(msg)

    with pytest.raises(KeyError, match="nope"):
        fn()


# --- Mutual exclusion: pre-built config + kwargs --------------------------


def test_config_and_kwargs_are_mutually_exclusive() -> None:
    """Passing both `config=` and per-field kwargs raises `TypeError`."""
    cfg = FallbackConfig(when=Match.exception(ValueError), default=1)
    with pytest.raises(TypeError, match="pre-built config OR"):
        Fallback("x", when=ValueError, config=cfg)
