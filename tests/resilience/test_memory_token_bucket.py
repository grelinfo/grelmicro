"""Tests for the standalone MemoryTokenBucket primitive."""

import threading
from time import monotonic

import pytest

from grelmicro.resilience.ratelimiter.memory import MemoryTokenBucket

pytestmark = [pytest.mark.timeout(2)]

CAPACITY = 3
SMALL_EPSILON = 0.001
THREAD_COUNT = 8
CALLS_PER_THREAD = 50
THROUGHPUT_CAPACITY = 100
THROUGHPUT_SLACK = 10


# --- Construction & properties ---


def test_properties() -> None:
    """Test properties expose config."""
    # Arrange
    capacity = 10
    refill_rate = 2.0

    # Act
    bucket = MemoryTokenBucket(capacity=capacity, refill_rate=refill_rate)

    # Assert
    assert bucket.capacity == capacity
    assert bucket.refill_rate == refill_rate


@pytest.mark.parametrize(
    ("capacity", "refill_rate"),
    [(0, 1), (-1, 1), (1, 0), (1, -1)],
)
def test_invalid_config(capacity: int, refill_rate: float) -> None:
    """Test non-positive capacity or refill_rate raises ValueError."""
    # Act & Assert
    with pytest.raises(ValueError, match="greater than"):
        MemoryTokenBucket(capacity=capacity, refill_rate=refill_rate)


# --- try_acquire ---


def test_starts_full() -> None:
    """Test bucket starts at capacity."""
    # Arrange
    bucket = MemoryTokenBucket(capacity=3, refill_rate=1)

    # Act
    results = [bucket.try_acquire() for _ in range(5)]

    # Assert
    assert results == [True, True, True, False, False]


def test_independent_keys() -> None:
    """Test different keys do not share tokens."""
    # Arrange
    bucket = MemoryTokenBucket(capacity=2, refill_rate=1)

    # Act
    bucket.try_acquire("a")
    bucket.try_acquire("a")
    a_third = bucket.try_acquire("a")
    b_first = bucket.try_acquire("b")

    # Assert
    assert a_third is False
    assert b_first is True


def test_cost() -> None:
    """Test cost consumes multiple tokens."""
    # Arrange
    bucket = MemoryTokenBucket(capacity=5, refill_rate=1)

    # Act
    allowed = bucket.try_acquire(cost=3)
    remaining = bucket.peek()

    # Assert
    assert allowed is True
    assert abs(remaining - 2.0) <= SMALL_EPSILON


def test_invalid_cost() -> None:
    """Test cost outside (0, capacity] raises ValueError."""
    # Arrange
    bucket = MemoryTokenBucket(capacity=5, refill_rate=1)

    # Act & Assert
    with pytest.raises(ValueError, match="cost must be in"):
        bucket.try_acquire(cost=0)
    with pytest.raises(ValueError, match="cost must be in"):
        bucket.try_acquire(cost=-1)
    with pytest.raises(ValueError, match="cost must be in"):
        bucket.try_acquire(cost=6)


# --- peek ---


def test_peek_does_not_consume() -> None:
    """Test peek does not mutate state."""
    # Arrange
    bucket = MemoryTokenBucket(capacity=3, refill_rate=1)

    # Act
    bucket.peek()
    bucket.peek()
    allowed = bucket.try_acquire()

    # Assert
    assert allowed is True


def test_peek_returns_tokens() -> None:
    """Test peek returns current token count."""
    # Arrange
    bucket = MemoryTokenBucket(capacity=5, refill_rate=0.01)

    # Act
    bucket.try_acquire(cost=3)
    tokens = bucket.peek()

    # Assert
    assert abs(tokens - 2.0) <= SMALL_EPSILON


# --- reset ---


def test_reset_restores_full_capacity() -> None:
    """Test reset clears state for a key."""
    # Arrange
    bucket = MemoryTokenBucket(capacity=CAPACITY, refill_rate=1)
    for _ in range(CAPACITY):
        bucket.try_acquire()

    # Act
    bucket.reset()
    tokens = bucket.peek()

    # Assert
    assert abs(tokens - CAPACITY) <= SMALL_EPSILON


def test_reset_only_affects_given_key() -> None:
    """Test reset on one key leaves others untouched."""
    # Arrange
    bucket = MemoryTokenBucket(capacity=3, refill_rate=0.01)
    for _ in range(3):
        bucket.try_acquire("a")
    for _ in range(2):
        bucket.try_acquire("b")

    # Act
    bucket.reset("a")

    # Assert
    assert abs(bucket.peek("a") - CAPACITY) <= SMALL_EPSILON
    assert abs(bucket.peek("b") - 1.0) <= SMALL_EPSILON


def test_reset_unknown_key_is_noop() -> None:
    """Test reset on a nonexistent key does nothing."""
    # Arrange
    bucket = MemoryTokenBucket(capacity=3, refill_rate=1)

    # Act (should not raise)
    bucket.reset("unknown")


# --- Thread-safety smoke test ---


def test_thread_safety_smoke() -> None:
    """Test concurrent callers do not exceed capacity under fast bursts."""
    # Arrange
    bucket = MemoryTokenBucket(capacity=THROUGHPUT_CAPACITY, refill_rate=1)
    results: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(CALLS_PER_THREAD):
            allowed = bucket.try_acquire(key="shared")
            with lock:
                results.append(allowed)

    # Act
    threads = [threading.Thread(target=worker) for _ in range(THREAD_COUNT)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Assert: capacity + small refill slack were granted, no more.
    granted = sum(1 for r in results if r)
    assert (
        THROUGHPUT_CAPACITY <= granted <= THROUGHPUT_CAPACITY + THROUGHPUT_SLACK
    )


# --- Eviction ---


def test_eviction_when_threshold_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test lazy eviction drops full-capacity keys past threshold."""
    # Arrange
    monkeypatch.setattr(
        "grelmicro.resilience.ratelimiter.memory._EVICTION_THRESHOLD", 2
    )
    bucket = MemoryTokenBucket(capacity=5, refill_rate=10)
    now = monotonic()
    # Seed two "old" entries whose refill has long reached capacity.
    bucket._state["full1"] = (5.0, now - 100.0)
    bucket._state["full2"] = (5.0, now - 100.0)

    # Act: third key crosses the threshold and triggers eviction.
    bucket.try_acquire("new")

    # Assert: already-full keys are gone; the just-used key stays.
    assert "full1" not in bucket._state
    assert "full2" not in bucket._state
    assert "new" in bucket._state
