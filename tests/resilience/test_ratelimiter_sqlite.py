"""Tests for the SQLite rate-limiter adapter specifics."""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from grelmicro.errors import OutOfContextError
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.resilience.ratelimiter import (
    RateLimiterConfig,
    SlidingWindowConfig,
    TokenBucketConfig,
)
from grelmicro.resilience.ratelimiter.sqlite import (
    SQLiteRateLimiterAdapter,
    _SQLiteTokenBucket,
)


@pytest.fixture
async def provider(tmp_path: Path) -> AsyncGenerator[SQLiteProvider]:
    """Open a SQLite provider on a temp file."""
    async with SQLiteProvider(tmp_path / "rate_limit.db") as backend:
        yield backend


@pytest.fixture
async def adapter(
    provider: SQLiteProvider,
) -> AsyncGenerator[SQLiteRateLimiterAdapter]:
    """Rate limiter adapter bound to the provider."""
    async with provider.ratelimiter() as backend:
        yield backend


def test_refill_clamps_clock_step_back() -> None:
    """A backwards wall-clock step never removes tokens from the bucket.

    `now < last` after an NTP step-back must not produce negative refill,
    which would transiently over-restrict the limiter.
    """
    # Arrange
    bucket = _SQLiteTokenBucket(
        conn=None,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        lock=None,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        prefix="grel:ratelimiter:test:",
        table_name="rate_limit",
        config=TokenBucketConfig(capacity=10, refill_rate=1.0),
    )

    # Act: last is in the future relative to now (clock stepped back 5s).
    refilled = bucket._refill(tokens=8.0, last=1005.0, now=1000.0)

    # Assert: tokens are unchanged, not reduced by negative elapsed.
    assert refilled == 8.0  # noqa: PLR2004


def test_invalid_table_name_raises() -> None:
    """Test an invalid SQL identifier is rejected."""
    with pytest.raises(ValueError, match="not a valid SQL identifier"):
        SQLiteRateLimiterAdapter(
            provider=SQLiteProvider("x.db"), table_name="bad name;"
        )


def test_no_provider_builds_implicit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `provider=`, the adapter builds one from `SQLITE_PATH`."""
    db_path = tmp_path / "from_env.db"
    monkeypatch.setenv("SQLITE_PATH", str(db_path))

    adapter = SQLiteRateLimiterAdapter()

    assert adapter.provider.path == str(db_path)
    assert adapter._owns_provider is True


def test_explicit_provider_is_borrowed() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    provider = SQLiteProvider("x.db")

    adapter = SQLiteRateLimiterAdapter(provider=provider)

    assert adapter.provider is provider
    assert adapter._owns_provider is False


def test_bind_before_open_raises() -> None:
    """Binding before the provider is open raises `OutOfContextError`."""
    adapter = SQLiteRateLimiterAdapter(provider=SQLiteProvider("x.db"))
    with pytest.raises(OutOfContextError):
        adapter.bind(TokenBucketConfig(capacity=5, refill_rate=1))


async def test_owned_provider_opens_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An implicit (owned) provider is opened on enter and closed on exit."""
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "owned.db"))
    adapter = SQLiteRateLimiterAdapter()  # builds and owns the provider
    async with adapter:
        strategy = adapter.bind(TokenBucketConfig(capacity=5, refill_rate=1))
        result = await strategy.acquire(key="k", cost=1)
    assert result.allowed is True
    # The owned provider is closed on exit.
    with pytest.raises(OutOfContextError):
        _ = adapter.provider.client


def test_rebind_provider_borrows() -> None:
    """`_rebind_provider` swaps the provider and marks it borrowed."""
    adapter = SQLiteRateLimiterAdapter(provider=SQLiteProvider("a.db"))
    shared = SQLiteProvider("shared.db")

    adapter._rebind_provider(shared)

    assert adapter.provider is shared
    assert adapter._owns_provider is False


@pytest.mark.parametrize(
    "config",
    [
        TokenBucketConfig(capacity=5, refill_rate=1),
        SlidingWindowConfig(limit=5, window=60.0),
    ],
)
async def test_acquire_rolls_back_on_error(
    adapter: SQLiteRateLimiterAdapter,
    config: RateLimiterConfig,
) -> None:
    """Test a failure inside the write transaction rolls back cleanly."""
    strategy = adapter.bind(config)
    conn = adapter.provider.client
    original_execute = conn.execute

    def fail_on_insert(sql: str, *args: object, **kwargs: object) -> object:
        if sql.lstrip().upper().startswith("INSERT"):
            msg = "boom"
            raise RuntimeError(msg)
        return original_execute(sql, *args, **kwargs)

    conn.execute = fail_on_insert  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
    try:
        with pytest.raises(RuntimeError, match="boom"):
            await strategy.acquire(key="rollback", cost=1)
    finally:
        conn.execute = original_execute  # type: ignore[method-assign]

    # The connection is usable again: the failed transaction rolled back.
    result = await strategy.acquire(key="rollback", cost=1)
    assert result.allowed is True
