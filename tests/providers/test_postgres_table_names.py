"""A Postgres table name that cuts a name built from it warns until 0.46.0."""

import inspect
import re
import warnings
from collections.abc import Callable

import pytest

from grelmicro._deprecation import long_table_name
from grelmicro.cache import postgres as cache_postgres
from grelmicro.coordination import postgres as coordination_postgres
from grelmicro.errors import (
    Grelmicro046Warning,
    GrelmicroFutureWarning,
    SettingsValidationError,
)
from grelmicro.outbox import postgres as outbox_postgres
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.resilience.circuitbreaker import (
    postgres as circuitbreaker_postgres,
)
from grelmicro.resilience.ratelimiter import postgres as ratelimiter_postgres
from grelmicro.task import postgres as task_postgres

_IDENTIFIER_MAX = 63
"""Postgres cuts an identifier past 63 bytes."""

_DERIVED = re.compile(r"\{table(?:_name)?\}(_\w+)")
"""A name the adapter builds from its table name, captured as the suffix.

It matches every index, function, side table and notify channel, in the
SQL and in the code that builds the channel name.
"""


def _longest_table_name(adapter: type) -> int:
    """Return the longest table name every name `adapter` builds keeps whole."""
    suffixes = _DERIVED.findall(inspect.getsource(adapter))
    assert suffixes
    return _IDENTIFIER_MAX - max(len(suffix.encode()) for suffix in suffixes)


def _provider() -> PostgresProvider:
    """Return a provider that never connects."""
    return PostgresProvider("postgresql://test:test@localhost:5432/test")


def _outbox_sql_table(name: str) -> str:
    """Return `name` when the outbox DDL creates a table of that name."""
    sql = outbox_postgres.PostgresOutboxAdapter.create_table_sql(name)
    return name if f"CREATE TABLE IF NOT EXISTS {name} (" in sql else ""


_ADAPTERS = [
    pytest.param(
        cache_postgres.PostgresCacheAdapter,
        "PostgresCacheAdapter",
        "table_name",
        lambda name: (
            cache_postgres.PostgresCacheAdapter(
                provider=_provider(), table_name=name
            )._table_name
        ),
        id="cache",
    ),
    pytest.param(
        outbox_postgres.PostgresOutboxAdapter,
        "PostgresOutboxAdapter",
        "table",
        lambda name: (
            outbox_postgres.PostgresOutboxAdapter(
                provider=_provider(), table=name
            )._table
        ),
        id="outbox",
    ),
    pytest.param(
        outbox_postgres.PostgresOutboxAdapter,
        "PostgresOutboxAdapter.create_table_sql",
        "table",
        _outbox_sql_table,
        id="outbox.create_table_sql",
    ),
    pytest.param(
        ratelimiter_postgres.PostgresRateLimiterAdapter,
        "PostgresRateLimiterAdapter",
        "table_name",
        lambda name: (
            ratelimiter_postgres.PostgresRateLimiterAdapter(
                provider=_provider(), table_name=name
            )._table_name
        ),
        id="ratelimiter",
    ),
    pytest.param(
        circuitbreaker_postgres.PostgresCircuitBreakerAdapter,
        "PostgresCircuitBreakerAdapter",
        "table_name",
        lambda name: (
            circuitbreaker_postgres.PostgresCircuitBreakerAdapter(
                provider=_provider(), table_name=name
            )._table_name
        ),
        id="circuitbreaker",
    ),
    pytest.param(
        coordination_postgres.PostgresReadWriteLockAdapter,
        "PostgresReadWriteLockAdapter",
        "table_name",
        lambda name: (
            coordination_postgres.PostgresReadWriteLockAdapter(
                provider=_provider(), table_name=name
            )._table_name
        ),
        id="rwlock",
    ),
    pytest.param(
        coordination_postgres.PostgresLeaderElectionAdapter,
        "PostgresLeaderElectionAdapter",
        "table_name",
        lambda name: (
            coordination_postgres.PostgresLeaderElectionAdapter(
                provider=_provider(), table_name=name
            )._table_name
        ),
        id="leader_election",
    ),
]
"""Each released way to name a Postgres table, with what it keeps of the name.

Each entry is the adapter class, how the warning names the call, the
argument that takes the name, and a call that builds with a name and returns
the table name it keeps.
"""


@pytest.mark.parametrize(("source", "adapter", "argument", "build"), _ADAPTERS)
def test_a_table_name_at_the_limit_warns_nothing(
    source: type,
    adapter: str,
    argument: str,
    build: Callable[[str], str],
) -> None:
    """The longest name whose built names all stay whole is quiet."""
    # Arrange
    name = "t" * _longest_table_name(source)

    # Act
    with warnings.catch_warnings():
        warnings.simplefilter("error", GrelmicroFutureWarning)
        kept = build(name)

    # Assert
    assert kept == name, (adapter, argument)


@pytest.mark.parametrize(("source", "adapter", "argument", "build"), _ADAPTERS)
def test_a_table_name_one_over_the_limit_warns_at_the_line_that_built_it(
    source: type,
    adapter: str,
    argument: str,
    build: Callable[[str], str],
) -> None:
    """One byte over warns once, at the user's line, and keeps the name."""
    # Arrange
    longest = _longest_table_name(source)
    name = "t" * (longest + 1)
    expected = long_table_name(
        name, adapter=adapter, argument=argument, longest=longest
    ).message

    # Act
    with pytest.warns(Grelmicro046Warning, match=re.escape(expected)) as caught:
        kept = build(name)

    # Assert
    assert [warning.filename for warning in caught] == [__file__]
    assert kept == name


def test_the_warning_names_the_value_the_adapter_and_the_limit() -> None:
    """The message says which name, on which adapter, and how long it may be."""
    # Arrange
    name = "c" * 49

    # Act
    message = long_table_name(
        name, adapter="PostgresCacheAdapter", argument="table_name", longest=48
    ).message

    # Assert
    assert message == (
        f"`PostgresCacheAdapter(table_name='{name}')` is longer than the 48 "
        "characters it takes, and raises `SettingsValidationError` from "
        "0.46.0. Until then Postgres cuts the names the adapter builds from "
        "it, such as its indexes and functions, at 63 bytes, so one can be "
        "left out or replace another with no error. Pass a `table_name` of 48 "
        "characters or fewer."
    )


@pytest.mark.parametrize(
    "adapter",
    [
        coordination_postgres.PostgresLockAdapter,
        coordination_postgres.PostgresScheduleAdapter,
    ],
)
def test_an_adapter_that_builds_no_name_from_its_table_has_no_limit(
    adapter: type,
) -> None:
    """The lock and schedule tables are the only names their adapters build."""
    assert _DERIVED.findall(inspect.getsource(adapter)) == []


def test_the_job_store_refuses_a_table_name_over_its_limit() -> None:
    """Jobs are unreleased, so the job store raises at once, at the same limit."""
    # Arrange
    longest = _longest_table_name(task_postgres.PostgresJobAdapter)

    # Act and assert
    task_postgres.PostgresJobAdapter(
        provider=_provider(), table_name="j" * longest
    )
    with pytest.raises(
        SettingsValidationError, match=f"longer than {longest} characters"
    ):
        task_postgres.PostgresJobAdapter(
            provider=_provider(), table_name="j" * (longest + 1)
        )
