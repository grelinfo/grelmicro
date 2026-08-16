"""The environment tunes an algorithm's fields and never selects the algorithm.

`RateLimiter` and `CircuitBreaker` carry a discriminated config union, so a
variable can name a field that belongs to an algorithm the code did not choose.
The rule is address-scoped:

- The instance address (`GREL_RATELIMITER_API_*`) names one object whose
  algorithm is known, so a foreign field is an unambiguous mistake and refuses
  to start.
- The kind address (`GREL_RATELIMITER_*`) is a broadcast. A fleet running both
  algorithms legitimately tunes its token buckets there while sliding-window
  limiters ignore what does not apply, so it stays silent.
"""

import pytest
from pydantic import BaseModel

from grelmicro._config import _running_kind, _union_arms
from grelmicro.coordination import Lock
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.errors import EnvLoadOffWarning, SettingsValidationError
from grelmicro.resilience import (
    ApiShieldConfig,
    CircuitBreaker,
    ConsecutiveCountConfig,
    InternalShieldConfig,
    RateLimiter,
    SlidingWindowConfig,
    SlowShieldConfig,
    TimeoutConfig,
    TokenBucketConfig,
)
from grelmicro.resilience.ratelimiter import _union_for_env
from grelmicro.resilience.timeout import Timeout

_CAPACITY = 50
_LIMIT = 100
_WINDOW = 60.0
_THRESHOLD = 9


def test_env_fills_a_field_the_caller_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A factory resolves missing fields from the instance address."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_RATELIMITER_API_CAPACITY", str(_CAPACITY))

    limiter = RateLimiter.token_bucket("api", refill_rate=1)

    assert isinstance(limiter.config, TokenBucketConfig)
    assert limiter.config.capacity == _CAPACITY


def test_circuit_breaker_reads_env_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same variable `ExternalConfig` retunes also seeds at startup."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv(
        "GREL_CIRCUITBREAKER_PAYMENTS_ERROR_THRESHOLD", str(_THRESHOLD)
    )

    assert CircuitBreaker("payments").config.error_threshold == _THRESHOLD
    assert (
        CircuitBreaker.consecutive_count("payments").config.error_threshold
        == _THRESHOLD
    )


def test_instance_address_refuses_another_algorithms_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign field at the instance address fails at construction."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_RATELIMITER_API_CAPACITY", str(_CAPACITY))

    with pytest.raises(SettingsValidationError, match="different algorithm"):
        RateLimiter.sliding_window("api", limit=_LIMIT, window=_WINDOW)


def test_kind_address_is_a_broadcast_and_stays_silent(
    monkeypatch: pytest.MonkeyPatch,
    recwarn: pytest.WarningsRecorder,
) -> None:
    """A mixed fleet tunes one algorithm kind-wide without noise."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_RATELIMITER_CAPACITY", str(_CAPACITY))

    bucket = RateLimiter.token_bucket("a", refill_rate=1)
    window = RateLimiter.sliding_window("b", limit=_LIMIT, window=_WINDOW)

    assert isinstance(bucket.config, TokenBucketConfig)
    assert isinstance(window.config, SlidingWindowConfig)
    assert bucket.config.capacity == _CAPACITY
    assert window.config.limit == _LIMIT
    assert not [
        w for w in recwarn.list if issubclass(w.category, EnvLoadOffWarning)
    ]


def test_gate_off_reports_another_algorithms_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the gate off, a foreign field is reported rather than dropped."""
    monkeypatch.delenv("GREL_ENV_LOAD", raising=False)
    monkeypatch.setenv("GREL_RATELIMITER_REPORTED_CAPACITY", str(_CAPACITY))

    with pytest.warns(EnvLoadOffWarning, match="CAPACITY"):
        RateLimiter.sliding_window("reported", limit=_LIMIT, window=_WINDOW)


def test_from_config_ignores_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`from_config` is the static door on both patterns."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv(
        "GREL_CIRCUITBREAKER_PAYMENTS_ERROR_THRESHOLD", str(_THRESHOLD)
    )

    breaker = CircuitBreaker.from_config("payments", ConsecutiveCountConfig())

    assert breaker.config.error_threshold != _THRESHOLD


async def test_env_built_instance_accepts_a_plain_config_on_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An env-built instance holds a settings subclass, not the plain config.

    `reconfigure` compares runtime types, so before this was fixed every
    instance constructed through the environment refused the config class
    its own docs told the caller to build.
    """
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_TIMEOUT_DB_SECONDS", "9")

    policy = Timeout("db")
    assert type(policy.config) is not TimeoutConfig

    await policy.reconfigure(TimeoutConfig(seconds=3.0))

    assert policy.config.seconds == 3.0  # noqa: PLR2004


def test_gate_off_reports_a_kind_wide_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kind-address variable is reported too, not just an instance one.

    The kind address applies to every instance since the bare prefix became
    the kind default, so a variable set there would have been read. Before
    this was fixed only the instance address was checked, so `R7` was false
    for every named instance in the library.
    """
    monkeypatch.delenv("GREL_ENV_LOAD", raising=False)
    monkeypatch.setenv("GREL_LOCK_LEASE_DURATION", "45")

    with pytest.warns(EnvLoadOffWarning, match="GREL_LOCK_LEASE_DURATION"):
        Lock("kindwide")


def test_union_arms_accepts_both_union_shapes() -> None:
    """The arm reader takes an annotated alias or a bare union.

    Both shipped patterns pass the annotated alias, but the helper is the
    seam a third-party pattern would use, and a bare union is the shape it
    is most likely to hand over.
    """
    annotated = _union_arms(_union_for_env())
    bare = _union_arms(TokenBucketConfig | SlidingWindowConfig)
    plain = _union_arms(TokenBucketConfig)

    assert set(annotated) == {TokenBucketConfig, SlidingWindowConfig}
    assert set(bare) == {TokenBucketConfig, SlidingWindowConfig}
    assert plain == ()


def test_shield_profiles_stay_presets_not_algorithms() -> None:
    """Pins the criterion that lets `GREL_SHIELD_{NAME}_PROFILE` exist.

    R6 permits a variable to choose between config classes only while every
    class declares the identical field names, because then the choice cannot
    make any variable start or stop applying. Add or remove a field on one
    profile and the choice becomes an algorithm selection, which belongs to
    code, so this test must fail rather than be updated.
    """
    api = set(ApiShieldConfig.model_fields)
    internal = set(InternalShieldConfig.model_fields)
    slow = set(SlowShieldConfig.model_fields)

    assert api == internal == slow

    # Contrast: the rate limiter arms differ, which is why the environment
    # may never choose between them.
    assert set(TokenBucketConfig.model_fields) != set(
        SlidingWindowConfig.model_fields
    )


def test_gate_off_does_not_call_a_broadcast_variable_a_mistake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kind-wide sibling field is legitimate, so the advice must not be removal.

    `GREL_RATELIMITER_CAPACITY` tunes every token bucket in the fleet. A
    sliding-window instance merely ignores it. Telling the operator to remove
    it would have them delete working configuration for the other algorithm.
    """
    monkeypatch.delenv("GREL_ENV_LOAD", raising=False)
    monkeypatch.setenv("GREL_RATELIMITER_CAPACITY", "50")

    with pytest.warns(EnvLoadOffWarning) as caught:
        RateLimiter.sliding_window(
            "broadcastadvice", limit=_LIMIT, window=_WINDOW
        )

    message = str(caught[0].message)
    assert "opt-in" in message
    assert "different algorithm" not in message


def test_a_bad_value_is_never_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rejected input never reaches the error text.

    A variable name is chosen by the operator and a value can be a
    credential, so the error names the variable and the reason and stops
    there. Raw pydantic errors carry `input_value`, which is why every
    construction path wraps.
    """
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_LOCK_LEAKY_LEASE_DURATION", "hunter2")

    with pytest.raises(SettingsValidationError) as caught:
        Lock("leaky")

    message = str(caught.value)
    assert "GREL_LOCK_LEAKY_LEASE_DURATION" in message
    assert "hunter2" not in message


def test_a_model_level_validator_error_renders() -> None:
    """An error with no field location renders instead of raising IndexError."""
    with pytest.raises(SettingsValidationError, match="retry_interval must be"):
        Lock("modelvalidator", backend=MemoryLockAdapter(), retry_interval=1e-9)


def test_running_kind_falls_back_to_the_class_name() -> None:
    """A union arm whose `kind` has no default still names itself.

    The message that rejects a foreign variable names the algorithm running.
    A third-party arm may declare `kind` as a required field rather than a
    defaulted literal, and rendering pydantic's sentinel there would put
    `PydanticUndefined` in front of an operator.
    """

    class NoDefaultKind(BaseModel):
        kind: str

    class NoKindAtAll(BaseModel):
        value: int = 1

    assert _running_kind(NoDefaultKind) == "NoDefaultKind"
    assert _running_kind(NoKindAtAll) == "NoKindAtAll"
