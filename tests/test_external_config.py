"""Tests for ExternalConfig and the live-reconfigure registration path."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self

import pytest

from grelmicro._config import (
    reconfigurable_instances,
    reconfigure_all,
    resolve_config_from_mapping,
)
from grelmicro.config import ConfigBackend, ExternalConfig, FileConfigAdapter
from grelmicro.config._external import (
    _coerce_source,
    _detect_scheme,
)
from grelmicro.coordination.lock import Lock
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.errors import AdapterNotRegisteredError
from grelmicro.resilience import (
    Bulkhead,
    CircuitBreaker,
    Fallback,
    RateLimiter,
    Retry,
    Timeout,
)
from grelmicro.resilience.ratelimiter import (
    SlidingWindowConfig,
    TokenBucketConfig,
)
from grelmicro.resilience.shield import Shield

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path
    from types import TracebackType

pytestmark = [pytest.mark.timeout(5)]


def _prefixes() -> set[str]:
    """Return the env prefixes of all live registered instances."""
    return {
        i._env_prefix
        for i in reconfigurable_instances()
        if i._env_prefix is not None
    }


# --- Task 1: registration reaches every named Reconfigurable ---


def test_circuitbreaker_and_ratelimiter_register_under_single_token_prefix() -> (
    None
):
    """CircuitBreaker and RateLimiter register under GREL_<MODULE>_<NAME>_."""
    cb = CircuitBreaker.consecutive_count("payments")
    rl = RateLimiter.sliding_window("api", limit=10, window=1.0)
    prefixes = _prefixes()
    assert "GREL_CIRCUITBREAKER_PAYMENTS_" in prefixes
    assert "GREL_RATELIMITER_API_" in prefixes
    # Keep references alive so the WeakSet does not drop them.
    assert cb.name == "payments"
    assert rl.name == "api"


def test_every_resilience_pattern_registers_from_constructor() -> None:
    """The seven owned patterns register from their live constructor path."""
    instances = [
        CircuitBreaker("orders"),
        RateLimiter.token_bucket("search", capacity=5, refill_rate=1.0),
        Retry.exponential("http", when=ValueError, env_load=False),
        Timeout("db", seconds=5, env_load=False),
        Bulkhead("io", max_concurrent=3, env_load=False),
        Fallback("cache", when=ValueError, default=None, env_load=False),
        Shield.api("svc"),
    ]
    prefixes = _prefixes()
    expected = {
        "GREL_CIRCUITBREAKER_ORDERS_",
        "GREL_RATELIMITER_SEARCH_",
        "GREL_RETRY_HTTP_",
        "GREL_TIMEOUT_DB_",
        "GREL_BULKHEAD_IO_",
        "GREL_FALLBACK_CACHE_",
        "GREL_SHIELD_SVC_",
    }
    assert expected <= prefixes
    assert len(instances) == 7  # noqa: PLR2004


def test_from_config_instances_stay_unregistered() -> None:
    """The declarative from_config path opts out of live reload."""
    before = {id(i) for i in reconfigurable_instances()}
    decls = [
        CircuitBreaker.from_config("decl_cb", CircuitBreaker("seed").config),
        RateLimiter.from_config(
            "decl_rl", TokenBucketConfig(capacity=2, refill_rate=1.0)
        ),
        Timeout.from_config("decl_to", Timeout("seed", seconds=1).config),
        Shield.from_config("decl_sh", Shield.api("seed").config),
    ]
    added = [i for i in reconfigurable_instances() if id(i) not in before]
    added_prefixes = {i._env_prefix for i in added}
    assert "GREL_CIRCUITBREAKER_DECL_CB_" not in added_prefixes
    assert "GREL_RATELIMITER_DECL_RL_" not in added_prefixes
    assert "GREL_TIMEOUT_DECL_TO_" not in added_prefixes
    assert "GREL_SHIELD_DECL_SH_" not in added_prefixes
    assert len(decls) == 4  # noqa: PLR2004


async def test_reconfigure_all_reaches_ratelimiter() -> None:
    """A mounted mapping addresses a RateLimiter by its name-as-namespace prefix."""
    rl = RateLimiter.sliding_window("orders", limit=10, window=1.0)
    await reconfigure_all(
        {"GREL_RATELIMITER_ORDERS_LIMIT": "99"},
    )
    assert isinstance(rl.config, SlidingWindowConfig)
    assert rl.config.limit == 99  # noqa: PLR2004


async def test_reconfigure_all_applies_mutable_beside_immutable_worker() -> (
    None
):
    """A co-located immutable `worker` key never drops the valid lease change.

    The mapping carries both the immutable identity field (`worker`) and a
    mutable one (`lease_duration`). The immutable key is skipped, so the
    lease change still applies instead of the whole instance being rejected.
    """
    # Arrange
    lock = Lock(
        backend=MemoryLockAdapter(),
        name="cart",
        worker="w1",
        lease_duration=15.0,
    )

    # Act
    await reconfigure_all(
        {
            "GREL_LOCK_CART_WORKER": "w2",
            "GREL_LOCK_CART_LEASE_DURATION": "30",
        },
    )

    # Assert: lease applied, worker left unchanged.
    assert lock.config.lease_duration == 30.0  # noqa: PLR2004
    assert str(lock.config.worker) == "w1"


# --- Task 2: resolve_config_from_mapping unmatched-key debug log ---


def test_resolve_config_from_mapping_logs_unmatched_prefixed_keys(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A prefixed key naming no field is logged at DEBUG for diagnosis."""
    cfg = TokenBucketConfig(capacity=5, refill_rate=1.0)
    with caplog.at_level(logging.DEBUG, logger="grelmicro"):
        out = resolve_config_from_mapping(
            cfg,
            env_prefix="GREL_RATELIMITER_API_",
            mapping={"GREL_RATELIMITER_API_TYPOED": "x"},
        )
    assert out is cfg
    assert "match no field" in caplog.text
    assert "GREL_RATELIMITER_API_" in caplog.text
    assert "TYPOED" not in caplog.text


# --- Task 2: log redaction never echoes offending values ---


async def test_reconfigure_all_never_logs_offending_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected value is logged by field and error type, never the value."""
    rl = RateLimiter.sliding_window("secretsvc", limit=10, window=1.0)
    secret = "s3cr3t-should-not-appear"
    with caplog.at_level(logging.WARNING, logger="grelmicro"):
        await reconfigure_all(
            {"GREL_RATELIMITER_SECRETSVC_LIMIT": secret},
        )
    assert "invalid external config" in caplog.text.lower()
    assert secret not in caplog.text
    assert isinstance(rl.config, SlidingWindowConfig)
    assert rl.config.limit == 10  # noqa: PLR2004


# --- Task 2: ExternalConfig.reload and error contract ---


class _RaisingBackend:
    """A ConfigBackend whose load always raises an unreadable-source error."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def load(self) -> Mapping[str, str] | None:
        msg = "mount not readable"
        raise OSError(msg)


class _ScriptedBackend:
    """A ConfigBackend that returns a queued mapping, or raises, per call."""

    def __init__(
        self, script: list[Mapping[str, str] | Exception | None]
    ) -> None:
        self._script = list(script)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def load(self) -> Mapping[str, str] | None:
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


async def test_reload_applies_immediately() -> None:
    """Reload performs one deterministic load-and-apply pass."""
    rl = RateLimiter.sliding_window("deterministic", limit=10, window=1.0)
    backend = _ScriptedBackend([{"GREL_RATELIMITER_DETERMINISTIC_LIMIT": "42"}])
    external = ExternalConfig(config=backend, interval=999.0)
    async with external:
        # The initial apply ran on __aenter__ already.
        assert isinstance(rl.config, SlidingWindowConfig)
        assert rl.config.limit == 42  # noqa: PLR2004


async def test_reload_keeps_last_good_config_on_adapter_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An adapter exception is logged and the last good config is kept."""
    rl = RateLimiter.sliding_window("resilient", limit=10, window=1.0)
    backend = _ScriptedBackend(
        [
            {"GREL_RATELIMITER_RESILIENT_LIMIT": "20"},
            OSError("transient read failure"),
        ]
    )
    external = ExternalConfig(config=backend, interval=999.0)
    async with external:
        assert isinstance(rl.config, SlidingWindowConfig)
        assert rl.config.limit == 20  # noqa: PLR2004
        with caplog.at_level(logging.WARNING, logger="grelmicro"):
            await external.reload()
        # The bad poll did not raise and did not change the config.
        assert isinstance(rl.config, SlidingWindowConfig)
        assert rl.config.limit == 20  # noqa: PLR2004
        assert "keeping last good config" in caplog.text


async def test_reload_raising_backend_does_not_raise() -> None:
    """A backend that always raises never propagates out of reload."""
    external = ExternalConfig(config=_RaisingBackend(), interval=999.0)
    async with external:
        await external.reload()  # must not raise


async def test_reload_failure_names_failing_config_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A config-source failure names the config source and keeps last good."""
    rl = RateLimiter.sliding_window("namedcfg", limit=10, window=1.0)
    config = _ScriptedBackend(
        [
            {"GREL_RATELIMITER_NAMEDCFG_LIMIT": "20"},
            OSError("config mount unreadable"),
        ]
    )
    secrets = _ScriptedBackend([{}, {}])
    external = ExternalConfig(config=config, secrets=secrets, interval=999.0)
    async with external:
        assert isinstance(rl.config, SlidingWindowConfig)
        assert rl.config.limit == 20  # noqa: PLR2004
        with caplog.at_level(logging.WARNING, logger="grelmicro"):
            await external.reload()  # must not raise
        assert "config source" in caplog.text
        assert "secrets" not in caplog.text
        assert "keeping last good config" in caplog.text
        # The bad config load did not change the running config.
        assert isinstance(rl.config, SlidingWindowConfig)
        assert rl.config.limit == 20  # noqa: PLR2004


async def test_reload_failure_names_failing_secrets_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A secrets-source failure names the secrets source, config still applies."""
    rl = RateLimiter.sliding_window("namedsec", limit=10, window=1.0)
    config = _ScriptedBackend(
        [
            {"GREL_RATELIMITER_NAMEDSEC_LIMIT": "20"},
            {"GREL_RATELIMITER_NAMEDSEC_LIMIT": "30"},
        ]
    )
    secrets = _ScriptedBackend([{}, OSError("secret mount unreadable")])
    external = ExternalConfig(config=config, secrets=secrets, interval=999.0)
    async with external:
        assert isinstance(rl.config, SlidingWindowConfig)
        assert rl.config.limit == 20  # noqa: PLR2004
        with caplog.at_level(logging.WARNING, logger="grelmicro"):
            await external.reload()  # must not raise
        assert "secrets source" in caplog.text
        assert "keeping last good config" in caplog.text
        # The config source still applied despite the secrets failure.
        assert isinstance(rl.config, SlidingWindowConfig)
        assert rl.config.limit == 30  # noqa: PLR2004


async def test_reload_failure_never_logs_source_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing source is named, never echoing the values it last carried."""
    secret_value = "s3cr3t-should-not-appear"
    config = _ScriptedBackend(
        [
            {"GREL_RATELIMITER_REDACTED_LIMIT": secret_value},
            OSError("config mount unreadable"),
        ]
    )
    external = ExternalConfig(config=config, interval=999.0)
    async with external:
        with caplog.at_level(logging.WARNING, logger="grelmicro"):
            await external.reload()  # must not raise
        assert "config source" in caplog.text
        assert secret_value not in caplog.text


class _TrackingBackend:
    """A ConfigBackend that records how often it is entered and exited."""

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> Self:
        self.entered += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.exited += 1

    async def load(self) -> Mapping[str, str] | None:
        return {}


class _RaiseOnEnterBackend:
    """A ConfigBackend that raises when entered."""

    async def __aenter__(self) -> Self:
        msg = "secret mount unreadable"
        raise OSError(msg)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def load(self) -> Mapping[str, str] | None:  # pragma: no cover
        return {}


async def test_aenter_closes_first_source_when_second_enter_fails() -> None:
    """A failure entering the secrets source unwinds the opened config source."""
    # Arrange
    config_src = _TrackingBackend()
    external = ExternalConfig(
        config=config_src, secrets=_RaiseOnEnterBackend(), interval=999.0
    )

    # Act
    with pytest.raises(OSError, match="secret mount unreadable"):
        await external.__aenter__()

    # Assert: the config source was entered and then closed, no leak.
    assert config_src.entered == 1
    assert config_src.exited == 1


# --- FileConfigAdapter behavior ---


async def test_file_adapter_absent_path_reads_empty(tmp_path: Path) -> None:
    """An absent path reads as an empty mapping, not an error."""
    adapter = FileConfigAdapter(tmp_path / "missing")
    assert await adapter.load() == {}


async def test_file_adapter_invalid_json_raises_value_error(
    tmp_path: Path,
) -> None:
    """A .json file that is not a flat object raises ValueError."""
    bad = tmp_path / "config.json"
    bad.write_text("[1, 2, 3]")
    adapter = FileConfigAdapter(bad)
    with pytest.raises(ValueError, match="mapping of keys"):
        await adapter.load()


async def test_file_adapter_directory_reads_keys(tmp_path: Path) -> None:
    """A mounted directory reads each file as one key."""
    (tmp_path / "GREL_RATELIMITER_API_LIMIT").write_text("7\n")
    adapter = FileConfigAdapter(tmp_path)
    data = await adapter.load()
    assert data == {"GREL_RATELIMITER_API_LIMIT": "7"}


def test_config_backend_protocol_accepts_adapters(tmp_path: Path) -> None:
    """FileConfigAdapter and the test backends satisfy ConfigBackend."""
    assert isinstance(FileConfigAdapter(tmp_path), ConfigBackend)
    assert isinstance(_RaisingBackend(), ConfigBackend)


# --- ExternalConfig source routing ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("http://config.svc/app", "http"),
        ("https://config.svc/app", "http"),
        ("HTTPS://CONFIG.SVC/app", "http"),
        ("git@github.com:org/repo.git", "git"),
        ("git+https://github.com/org/repo", "git"),
        ("ssh://git@host/org/repo", "git"),
        ("host.example/org/repo.git", "git"),
        ("/etc/grelmicro", "file"),
        ("config.env", "file"),
    ],
)
def test_detect_scheme(text: str, expected: str) -> None:
    """Scheme detection routes URLs and falls back to the filesystem."""
    assert _detect_scheme(text) == expected


def test_coerce_source_path_builds_file_adapter(tmp_path: Path) -> None:
    """A local path string builds a FileConfigAdapter."""
    assert isinstance(_coerce_source(str(tmp_path)), FileConfigAdapter)


def test_coerce_source_passes_through_backend() -> None:
    """A ready ConfigBackend is returned unchanged."""
    backend = _RaisingBackend()
    assert _coerce_source(backend) is backend


def test_coerce_source_network_scheme_without_extra_raises() -> None:
    """A URL with no installed adapter raises AdapterNotRegisteredError."""
    with pytest.raises(AdapterNotRegisteredError):
        _coerce_source("https://config.svc/app")


def test_external_config_requires_a_source() -> None:
    """Neither config nor secrets given raises ValueError."""
    with pytest.raises(ValueError, match="requires a config source"):
        ExternalConfig()


# --- ExternalConfig merge, secrets, and poll loop ---


async def test_secrets_override_config_on_collision() -> None:
    """The secrets source wins over config on a shared key."""
    rl = RateLimiter.sliding_window("merged", limit=10, window=1.0)
    config = _ScriptedBackend([{"GREL_RATELIMITER_MERGED_LIMIT": "1"}])
    secrets = _ScriptedBackend([{"GREL_RATELIMITER_MERGED_LIMIT": "2"}])
    async with ExternalConfig(config=config, secrets=secrets, interval=999.0):
        assert isinstance(rl.config, SlidingWindowConfig)
        assert rl.config.limit == 2  # noqa: PLR2004


async def test_secrets_only_source_applies() -> None:
    """A secrets-only ExternalConfig applies with no config source."""
    rl = RateLimiter.sliding_window("secretsonly", limit=10, window=1.0)
    secrets = _ScriptedBackend([{"GREL_RATELIMITER_SECRETSONLY_LIMIT": "7"}])
    async with ExternalConfig(secrets=secrets, interval=999.0):
        assert isinstance(rl.config, SlidingWindowConfig)
        assert rl.config.limit == 7  # noqa: PLR2004


async def test_config_only_source_applies() -> None:
    """A config-only ExternalConfig applies with no secrets source."""
    rl = RateLimiter.sliding_window("configonly", limit=10, window=1.0)
    config = _ScriptedBackend([{"GREL_RATELIMITER_CONFIGONLY_LIMIT": "8"}])
    async with ExternalConfig(config=config, interval=999.0):
        assert isinstance(rl.config, SlidingWindowConfig)
        assert rl.config.limit == 8  # noqa: PLR2004


async def test_aexit_before_aenter_is_a_noop() -> None:
    """Closing an ExternalConfig that never opened does not raise."""
    external = ExternalConfig(config=_RaisingBackend(), interval=999.0)
    await external.__aexit__(None, None, None)


async def test_load_merged_skips_apply_when_no_source_has_data() -> None:
    """No source with data yet returns None and applies nothing."""
    rl = RateLimiter.sliding_window("nodata", limit=10, window=1.0)
    config = _ScriptedBackend([None, None])
    async with ExternalConfig(config=config, interval=999.0) as external:
        await external.reload()
        assert isinstance(rl.config, SlidingWindowConfig)
        assert rl.config.limit == 10  # noqa: PLR2004


async def test_secrets_unchanged_keeps_config_only_merge() -> None:
    """A secrets source reporting None still merges the config data."""
    rl = RateLimiter.sliding_window("mixmerge", limit=10, window=1.0)
    config = _ScriptedBackend([{"GREL_RATELIMITER_MIXMERGE_LIMIT": "4"}])
    secrets = _ScriptedBackend([None])
    async with ExternalConfig(config=config, secrets=secrets, interval=999.0):
        assert isinstance(rl.config, SlidingWindowConfig)
        assert rl.config.limit == 4  # noqa: PLR2004


async def test_unchanged_source_keeps_last_seen_mapping() -> None:
    """A source reporting None reuses its last seen mapping."""
    rl = RateLimiter.sliding_window("sticky", limit=10, window=1.0)
    config = _ScriptedBackend([{"GREL_RATELIMITER_STICKY_LIMIT": "5"}, None])
    async with ExternalConfig(config=config, interval=999.0) as external:
        assert isinstance(rl.config, SlidingWindowConfig)
        assert rl.config.limit == 5  # noqa: PLR2004
        await external.reload()
        assert isinstance(rl.config, SlidingWindowConfig)
        assert rl.config.limit == 5  # noqa: PLR2004


async def test_poll_loop_applies_on_each_interval() -> None:
    """The background poll loop applies the next scripted value."""
    rl = RateLimiter.sliding_window("polled", limit=10, window=1.0)
    config = _ScriptedBackend(
        [
            {"GREL_RATELIMITER_POLLED_LIMIT": "1"},
            {"GREL_RATELIMITER_POLLED_LIMIT": "2"},
            {"GREL_RATELIMITER_POLLED_LIMIT": "3"},
        ]
    )
    async with ExternalConfig(config=config, interval=0.001):
        for _ in range(200):
            await asyncio.sleep(0.005)
            assert isinstance(rl.config, SlidingWindowConfig)
            if rl.config.limit > 1:
                break
        assert isinstance(rl.config, SlidingWindowConfig)
        assert rl.config.limit > 1


def test_resolve_config_from_mapping_ignores_unprefixed_keys() -> None:
    """A key outside the prefix is skipped without logging or validation."""
    cfg = TokenBucketConfig(capacity=5, refill_rate=1.0)
    out = resolve_config_from_mapping(
        cfg,
        env_prefix="GREL_RATELIMITER_API_",
        mapping={"OTHER_PREFIX_CAPACITY": "9"},
    )
    assert out is cfg
