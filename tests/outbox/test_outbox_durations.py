"""Outbox leases and retention take whole seconds or a `timedelta`."""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid7

import pytest
from pydantic import ValidationError

from grelmicro._duration import MAX_DURATION, MICROSECOND, microseconds
from grelmicro.errors import SettingsValidationError
from grelmicro.outbox import Message, Outbox, OutboxConfig
from grelmicro.outbox._message import OutboxRecord
from grelmicro.outbox.memory import MemoryOutboxAdapter
from grelmicro.outbox.postgres import PostgresOutboxAdapter
from grelmicro.providers.postgres import PostgresProvider
from tests.outbox.spies import PurgeSpy

pytestmark = [pytest.mark.timeout(60)]

UNDER_A_SECOND = timedelta(microseconds=333_333)
"""A duration no float of seconds writes exactly."""

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
"""The memory backend clock in the boundary tests."""

WINDOW = timedelta(hours=1)
"""The retention window of the Postgres purge test."""

MARGIN = timedelta(seconds=30)
"""How far inside the window the kept row sits, covering the test's own run."""

LONGEST = MAX_DURATION - MICROSECOND
"""A hundred years less a microsecond, the hardest duration to keep exact."""

NOT_A_BOOL = (
    "0 to delete on delivery, or None .'none' from text. to keep forever"
)
"""What a refused bool `keep_delivered` says, as a pattern."""

REFUSED = [
    pytest.param(1.5, id="float"),
    pytest.param(60.0, id="whole-float"),
    pytest.param(True, id="bool"),
]

READ_BACK = [
    pytest.param(60, timedelta(seconds=60), id="whole-seconds"),
    pytest.param(UNDER_A_SECOND, UNDER_A_SECOND, id="under-a-second"),
]

FROM_TEXT = [
    pytest.param("60", timedelta(seconds=60), id="whole-seconds"),
    pytest.param("PT0.5S", timedelta(milliseconds=500), id="iso-8601"),
]


class _CompleteSpy(MemoryOutboxAdapter):
    """Memory backend that signals once a delivery is settled."""

    def __init__(self) -> None:
        super().__init__()
        self.completed = asyncio.Event()

    async def complete(
        self, *, message_id: UUID, attempts: int, keep: bool
    ) -> None:
        await super().complete(
            message_id=message_id, attempts=attempts, keep=keep
        )
        self.completed.set()


def _record() -> OutboxRecord:
    """Return a minimal record."""
    return OutboxRecord(id=uuid7(), topic="job", payload={"n": 1})


async def _claim_at(
    backend: MemoryOutboxAdapter, when: datetime, lease: timedelta
) -> list[OutboxRecord]:
    """Claim every due `job` message with the backend clock at `when`."""
    with patch("grelmicro.outbox.memory._now", return_value=when):
        return await backend.claim(topics=["job"], limit=10, lease=lease)


async def _purge_at(
    backend: MemoryOutboxAdapter, when: datetime, older_than: timedelta
) -> int:
    """Purge delivered rows older than `older_than` with the clock at `when`."""
    with patch("grelmicro.outbox.memory._now", return_value=when):
        return await backend.purge(older_than=older_than, states=("delivered",))


def _mocked_postgres() -> tuple[PostgresOutboxAdapter, MagicMock]:
    """Return an adapter on a mocked pool, and that pool."""
    provider = PostgresProvider("postgresql://user:pass@localhost:5432/db")
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.execute = AsyncMock(return_value="DELETE 0")
    provider._pool = pool
    return PostgresOutboxAdapter(provider=provider), pool


@pytest.mark.parametrize(("value", "expected"), READ_BACK)
def test_outbox_lease_duration_reads_back_as_timedelta(
    value: int | timedelta, expected: timedelta
) -> None:
    """Whole seconds and a `timedelta` are taken as the lease."""
    # Act
    outbox = Outbox(MemoryOutboxAdapter(), lease_duration=value, env_load=False)

    # Assert
    assert outbox.config.lease_duration == expected


@pytest.mark.parametrize("value", REFUSED)
def test_outbox_float_or_bool_lease_duration_refused(value: object) -> None:
    """A float or a bool lease is refused, naming `lease_duration`."""
    # Act / Assert
    with pytest.raises(
        SettingsValidationError,
        match="lease_duration must be whole seconds or a timedelta",
    ):
        Outbox(
            MemoryOutboxAdapter(),
            lease_duration=value,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            env_load=False,
        )


@pytest.mark.parametrize(("raw", "expected"), FROM_TEXT)
def test_outbox_lease_duration_from_environment_reads_as_timedelta(
    raw: str, expected: timedelta, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env text is whole seconds or an ISO 8601 duration."""
    # Arrange
    monkeypatch.setenv("GREL_OUTBOX_LEASE_DURATION", raw)

    # Act
    outbox = Outbox(MemoryOutboxAdapter(), env_load=True)

    # Assert
    assert outbox.config.lease_duration == expected


def test_outbox_decimal_lease_duration_from_environment_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decimal number of seconds from the environment is refused."""
    # Arrange
    monkeypatch.setenv("GREL_OUTBOX_LEASE_DURATION", "0.5")

    # Act / Assert
    with pytest.raises(SettingsValidationError, match="lease_duration"):
        Outbox(MemoryOutboxAdapter(), env_load=True)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        *READ_BACK,
        pytest.param(1, timedelta(seconds=1), id="one-second"),
        pytest.param(0, timedelta(0), id="zero"),
        pytest.param(None, None, id="forever"),
    ],
)
def test_outbox_keep_delivered_reads_back_as_window_zero_or_none(
    value: int | timedelta | None, expected: timedelta | None
) -> None:
    """A duration, zero and `None` are taken as given."""
    # Act
    outbox = Outbox(MemoryOutboxAdapter(), keep_delivered=value, env_load=False)

    # Assert
    assert outbox.config.keep_delivered == expected


def test_outbox_keep_delivered_none_wins_over_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `None` passed in code keeps rows for good whatever the environment says."""
    # Arrange
    monkeypatch.setenv("GREL_OUTBOX_KEEP_DELIVERED", "60")

    # Act
    outbox = Outbox(MemoryOutboxAdapter(), keep_delivered=None, env_load=True)

    # Assert
    assert outbox.config.keep_delivered is None


def test_outbox_keep_delivered_default_deletes_on_delivery() -> None:
    """Left out, `keep_delivered` is zero, so delivered rows are deleted."""
    # Act
    outbox = Outbox(MemoryOutboxAdapter(), env_load=False)

    # Assert
    assert outbox.config.keep_delivered == timedelta(0)


@pytest.mark.parametrize("value", [True, False])
def test_outbox_bool_keep_delivered_refused_naming_the_new_spellings(
    value: bool,  # noqa: FBT001
) -> None:
    """A bool is refused, and the message says to write `0` or `None`."""
    # Act / Assert
    with pytest.raises(SettingsValidationError, match=NOT_A_BOOL):
        Outbox(MemoryOutboxAdapter(), keep_delivered=value, env_load=False)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(1.5, id="float"),
        pytest.param(60.0, id="whole-float"),
    ],
)
def test_outbox_float_keep_delivered_refused(value: float) -> None:
    """A float retention window is refused, naming `keep_delivered`."""
    # Act / Assert
    with pytest.raises(
        SettingsValidationError,
        match="keep_delivered must be whole seconds or a timedelta",
    ):
        Outbox(
            MemoryOutboxAdapter(),
            keep_delivered=value,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            env_load=False,
        )


def test_outbox_negative_keep_delivered_refused() -> None:
    """A negative retention window is refused."""
    # Act / Assert
    with pytest.raises(
        ValidationError, match="keep_delivered must not be negative"
    ):
        OutboxConfig(keep_delivered=timedelta(seconds=-1))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        *FROM_TEXT,
        pytest.param("1", timedelta(seconds=1), id="one-second"),
        pytest.param("0", timedelta(0), id="zero"),
        pytest.param("PT0S", timedelta(0), id="iso-zero"),
        pytest.param("none", None, id="forever"),
        pytest.param("None", None, id="forever-capitalized"),
    ],
)
def test_outbox_keep_delivered_from_environment_reads_window_zero_or_forever(
    raw: str, expected: timedelta | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env text is a duration, zero, or `none` to keep rows for good."""
    # Arrange
    monkeypatch.setenv("GREL_OUTBOX_KEEP_DELIVERED", raw)

    # Act
    outbox = Outbox(MemoryOutboxAdapter(), env_load=True)

    # Assert
    assert outbox.config.keep_delivered == expected


@pytest.mark.parametrize(
    "raw",
    [
        "true",
        "False",
        "yes",
        "no",
        "on",
        "OFF",
        "t",
        "F",
        "y",
        "N",
        pytest.param(" true ", id="padded"),
    ],
)
def test_outbox_bool_keep_delivered_from_environment_refused(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bool spelling in the environment fails loudly, naming `0` and `none`."""
    # Arrange
    monkeypatch.setenv("GREL_OUTBOX_KEEP_DELIVERED", raw)

    # Act / Assert
    with pytest.raises(SettingsValidationError, match=NOT_A_BOOL):
        Outbox(MemoryOutboxAdapter(), env_load=True)


def test_outbox_decimal_keep_delivered_from_environment_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decimal number of seconds from the environment is refused."""
    # Arrange
    monkeypatch.setenv("GREL_OUTBOX_KEEP_DELIVERED", "0.5")

    # Act / Assert
    with pytest.raises(SettingsValidationError, match="keep_delivered"):
        Outbox(MemoryOutboxAdapter(), env_load=True)


JSON_RETENTION = [
    pytest.param(timedelta(days=400), "P400D", id="window"),
    pytest.param(timedelta(0), "PT0S", id="zero"),
    pytest.param(None, None, id="forever"),
]


@pytest.mark.parametrize(("keep_delivered", "expected"), JSON_RETENTION)
def test_outbox_config_durations_dump_to_json_as_iso_8601(
    keep_delivered: timedelta | None, expected: str | None
) -> None:
    """The lease and the retention dump as ISO 8601."""
    # Arrange
    config = OutboxConfig(
        lease_duration=timedelta(milliseconds=500),
        keep_delivered=keep_delivered,
    )

    # Act
    dumped = config.model_dump(mode="json")

    # Assert
    assert (dumped["lease_duration"], dumped["keep_delivered"]) == (
        "PT0.5S",
        expected,
    )


@pytest.mark.parametrize(("keep_delivered", "expected"), JSON_RETENTION)
def test_outbox_config_durations_read_back_from_json(
    keep_delivered: timedelta | None, expected: str | None
) -> None:
    """The ISO 8601 a config dumps reads back to the same config."""
    # Arrange
    config = OutboxConfig(
        lease_duration=timedelta(milliseconds=500),
        keep_delivered=keep_delivered,
    )

    # Act
    loaded = OutboxConfig.model_validate(
        {"lease_duration": "PT0.5S", "keep_delivered": expected}
    )

    # Assert
    assert loaded == config


async def _deliver_one(keep_delivered: int | timedelta | None) -> _CompleteSpy:
    """Deliver one message through a running relay, return its backend."""
    backend = _CompleteSpy()
    outbox = Outbox(
        backend,
        poll_interval=0.05,
        keep_delivered=keep_delivered,
        env_load=False,
    )

    @outbox.handler("job")
    async def handle(message: Message[object]) -> None:
        """Take the message."""

    async with outbox:
        await outbox.publish(None, "job", {})
        await asyncio.wait_for(backend.completed.wait(), 5)
    return backend


async def test_relay_zero_keep_delivered_deletes_the_row_on_delivery() -> None:
    """With `keep_delivered=0` a delivered row is deleted."""
    # Act
    backend = await _deliver_one(0)

    # Assert
    assert backend._rows == {}


@pytest.mark.parametrize(
    "keep_delivered",
    [
        pytest.param(None, id="forever"),
        pytest.param(timedelta(days=1), id="window"),
    ],
)
async def test_relay_none_or_window_keep_delivered_keeps_the_delivered_row(
    keep_delivered: timedelta | None,
) -> None:
    """With `None` or a window a delivered row stays, marked delivered."""
    # Act
    backend = await _deliver_one(keep_delivered)

    # Assert
    assert [row.state for row in backend._rows.values()] == ["delivered"]


async def test_relay_keep_delivered_none_never_purges() -> None:
    """With `None` no janitor runs, so delivered rows are never purged."""
    # Arrange
    backend = PurgeSpy()
    outbox = Outbox(
        backend, poll_interval=0.05, keep_delivered=None, env_load=False
    )

    # Act
    async with outbox:
        await asyncio.sleep(0.1)

    # Assert
    assert backend.calls == []


@pytest.mark.parametrize("value", [*REFUSED, pytest.param("60", id="text")])
async def test_outbox_purge_float_bool_or_text_older_than_refused(
    value: object,
) -> None:
    """A float, a bool or text is refused, naming `older_than`."""
    # Arrange
    outbox = Outbox(MemoryOutboxAdapter(), relay=False, env_load=False)

    # Act / Assert
    with pytest.raises(
        ValueError, match="older_than must be whole seconds or a timedelta"
    ):
        await outbox.purge(older_than=value)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


async def test_outbox_purge_whole_seconds_older_than_reaches_backend_as_timedelta() -> (
    None
):
    """Whole seconds reach the backend as a `timedelta`."""
    # Arrange
    backend = PurgeSpy()
    outbox = Outbox(backend, relay=False, env_load=False)

    # Act
    await outbox.purge(older_than=3600)

    # Assert
    assert backend.calls == [(timedelta(hours=1), ("delivered", "dead"))]


async def test_outbox_purge_zero_older_than_purges_every_delivered_row() -> (
    None
):
    """`older_than=0` keeps nothing, so every delivered row is purged."""
    # Arrange
    backend = MemoryOutboxAdapter()
    outbox = Outbox(backend, relay=False, env_load=False)
    with patch("grelmicro.outbox.memory._now", return_value=NOW):
        await backend.enqueue(None, _record())
        (claimed,) = await backend.claim(
            topics=["job"], limit=10, lease=UNDER_A_SECOND
        )
        await backend.complete(
            message_id=claimed.id, attempts=claimed.attempts, keep=True
        )

    # Act
    with patch("grelmicro.outbox.memory._now", return_value=NOW + MICROSECOND):
        purged = await outbox.purge(older_than=0)

    # Assert
    assert purged == 1


async def test_outbox_purge_negative_older_than_refused() -> None:
    """A negative age is refused, naming `older_than`."""
    # Arrange
    outbox = Outbox(MemoryOutboxAdapter(), relay=False, env_load=False)

    # Act / Assert
    with pytest.raises(ValueError, match="older_than must not be negative"):
        await outbox.purge(older_than=timedelta(seconds=-1))


async def test_memory_outbox_claimed_message_not_reclaimed_before_lease_ends() -> (
    None
):
    """A claim holds through the last microsecond of its lease."""
    # Arrange
    backend = MemoryOutboxAdapter()
    with patch("grelmicro.outbox.memory._now", return_value=NOW):
        await backend.enqueue(None, _record())
    await _claim_at(backend, NOW, UNDER_A_SECOND)

    # Act
    last_microsecond = await _claim_at(
        backend, NOW + UNDER_A_SECOND - MICROSECOND, UNDER_A_SECOND
    )
    at_lease_end = await _claim_at(
        backend, NOW + UNDER_A_SECOND, UNDER_A_SECOND
    )

    # Assert
    assert (len(last_microsecond), len(at_lease_end)) == (0, 1)


async def test_memory_outbox_delivered_message_kept_until_keep_delivered_ends() -> (
    None
):
    """A delivered row outlives its retention window before it is purged."""
    # Arrange
    backend = MemoryOutboxAdapter()
    with patch("grelmicro.outbox.memory._now", return_value=NOW):
        await backend.enqueue(None, _record())
    (claimed,) = await _claim_at(backend, NOW, UNDER_A_SECOND)
    with patch("grelmicro.outbox.memory._now", return_value=NOW):
        await backend.complete(
            message_id=claimed.id, attempts=claimed.attempts, keep=True
        )

    # Act
    at_window_end = await _purge_at(
        backend, NOW + UNDER_A_SECOND, UNDER_A_SECOND
    )
    past_window = await _purge_at(
        backend, NOW + UNDER_A_SECOND + MICROSECOND, UNDER_A_SECOND
    )

    # Assert
    assert (at_window_end, past_window) == (0, 1)


@pytest.mark.parametrize(
    ("lease", "expected"),
    [
        pytest.param(MICROSECOND, 1, id="one-microsecond"),
        pytest.param(UNDER_A_SECOND, 333_333, id="under-a-second"),
        pytest.param(MAX_DURATION, 3_153_600_000_000_000, id="max"),
    ],
)
async def test_postgres_outbox_claim_passes_lease_in_whole_microseconds(
    lease: timedelta, expected: int
) -> None:
    """`claim` passes the lease as exact whole microseconds."""
    # Arrange
    backend, pool = _mocked_postgres()

    # Act
    await backend.claim(topics=["job"], limit=10, lease=lease)

    # Assert
    pool.fetch.assert_awaited_once_with(
        backend._claim_sql, ["job"], 10, expected
    )


@pytest.mark.parametrize(
    ("older_than", "expected"),
    [
        pytest.param(UNDER_A_SECOND, 333_333, id="under-a-second"),
        pytest.param(None, None, id="all"),
    ],
)
async def test_postgres_outbox_purge_passes_older_than_in_whole_microseconds(
    older_than: timedelta | None, expected: int | None
) -> None:
    """`purge` passes the age as exact whole microseconds, or none."""
    # Arrange
    backend, pool = _mocked_postgres()

    # Act
    await backend.purge(older_than=older_than)

    # Assert
    pool.execute.assert_awaited_once_with(
        backend._purge_sql, expected, ["delivered", "dead"]
    )


@pytest.fixture(scope="module")
async def postgres() -> AsyncGenerator[PostgresOutboxAdapter]:
    """Provide an outbox adapter on a Postgres container."""
    from testcontainers.community.postgres import (  # noqa: PLC0415
        PostgresContainer,
    )

    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        url = f"postgresql://test:test@localhost:{port}/test"
        async with (
            PostgresProvider(url) as provider,
            PostgresOutboxAdapter(provider=provider, notify=False) as backend,
        ):
            yield backend


async def _stage(backend: PostgresOutboxAdapter, topic: str) -> None:
    """Stage one message on `topic`."""
    record = OutboxRecord(id=uuid7(), topic=topic, payload={})
    async with (
        backend.provider.client.acquire() as conn,
        conn.transaction(),
    ):
        await backend.enqueue(conn, record)


@pytest.mark.integration
async def test_postgres_outbox_claim_lease_ends_exactly_its_duration_later(
    postgres: PostgresOutboxAdapter,
) -> None:
    """A claim's lease ends its exact duration after the claim."""
    # Arrange
    topic = uuid7().hex
    await _stage(postgres, topic)
    async with (
        postgres.provider.client.acquire() as conn,
        conn.transaction(),
    ):
        # Act
        (row,) = await conn.fetch(
            postgres._claim_sql, [topic], 10, microseconds(LONGEST)
        )
        claimed_at = await conn.fetchval("SELECT NOW();")
        available_at = await conn.fetchval(
            "SELECT available_at FROM grelmicro_outbox WHERE id = $1;",
            row["id"],
        )

    # Assert
    assert available_at - claimed_at == LONGEST


@pytest.mark.integration
async def test_postgres_outbox_claimed_message_not_reclaimed_before_lease_ends(
    postgres: PostgresOutboxAdapter,
) -> None:
    """A claimed message stays invisible while its lease runs."""
    # Arrange
    topic = uuid7().hex
    await _stage(postgres, topic)
    await postgres.claim(topics=[topic], limit=10, lease=timedelta(minutes=1))

    # Act
    reclaimed = await postgres.claim(
        topics=[topic], limit=10, lease=timedelta(minutes=1)
    )

    # Assert
    assert reclaimed == []


async def _deliver_aged(
    backend: PostgresOutboxAdapter,
    topics: Sequence[str],
    ages: Sequence[timedelta],
) -> None:
    """Mark one message per topic delivered, as old as given."""
    async with (
        backend.provider.client.acquire() as conn,
        conn.transaction(),
    ):
        for topic, age in zip(topics, ages, strict=True):
            await conn.execute(
                "UPDATE grelmicro_outbox SET state = 'delivered', "
                "delivered_at = NOW() - $2::bigint * INTERVAL '1 microsecond' "
                "WHERE topic = $1;",
                topic,
                microseconds(age),
            )


@pytest.mark.integration
async def test_postgres_outbox_delivered_message_kept_until_keep_delivered_ends(
    postgres: PostgresOutboxAdapter,
) -> None:
    """A delivered row inside the window is kept, one past it is purged."""
    # Arrange
    inside, past = uuid7().hex, uuid7().hex
    await _stage(postgres, inside)
    await _stage(postgres, past)
    await _deliver_aged(
        postgres, [inside, past], [WINDOW - MARGIN, WINDOW + MICROSECOND]
    )

    # Act
    purged = await postgres.purge(older_than=WINDOW, states=("delivered",))

    # Assert
    assert purged == 1
