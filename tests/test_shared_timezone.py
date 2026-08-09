"""Test the app-wide `GREL_TIMEZONE` variable."""

from datetime import timedelta

import pytest

from grelmicro._config import resolve_config, resolve_config_from_mapping
from grelmicro._timezone import (
    SHARED_TIMEZONE_ENV,
    normalize_timezone_name,
    resolve_timezone,
)
from grelmicro.errors import GrelmicroConfigWarning
from grelmicro.log._shared import load_settings
from grelmicro.log.config import LogConfig
from grelmicro.task import Tasks, TasksConfig

SHUTDOWN_TIMEOUT = 5


@pytest.fixture(autouse=True)
def _enable_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the environment path on for the whole module."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")


def _tasks_config(**kwargs: object) -> TasksConfig:
    """Resolve a `TasksConfig` the way `Tasks` does."""
    return resolve_config(
        TasksConfig,
        explicit=None,
        kwargs=kwargs,
        env_prefix="GREL_TASK_",
        shared_env=SHARED_TIMEZONE_ENV,
    )


def test_shared_variable_fills_an_unset_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GREL_TIMEZONE` applies when the component variable is unset."""
    # Arrange
    monkeypatch.setenv("GREL_TIMEZONE", "Europe/Zurich")

    # Act / Assert
    assert _tasks_config().timezone == "Europe/Zurich"


def test_component_variable_beats_the_shared_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A component variable is more specific, so it wins."""
    # Arrange
    monkeypatch.setenv("GREL_TIMEZONE", "Europe/Zurich")
    monkeypatch.setenv("GREL_TASK_TIMEZONE", "America/Chicago")

    # Act / Assert
    assert _tasks_config().timezone == "America/Chicago"


def test_keyword_argument_beats_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value passed in code always wins."""
    # Arrange
    monkeypatch.setenv("GREL_TIMEZONE", "Europe/Zurich")
    monkeypatch.setenv("GREL_TASK_TIMEZONE", "America/Chicago")

    # Act / Assert
    assert _tasks_config(timezone="Asia/Tokyo").timezone == "Asia/Tokyo"


def test_lower_case_component_variable_still_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matching is delegated, so casing behaves as it does everywhere else."""
    # Arrange
    monkeypatch.setenv("GREL_TIMEZONE", "Europe/Zurich")
    monkeypatch.setenv("grel_task_timezone", "America/Chicago")

    # Act / Assert
    assert _tasks_config().timezone == "America/Chicago"


def test_shared_variable_falls_back_to_utc() -> None:
    """Nothing set anywhere keeps the default."""
    # Act / Assert
    assert _tasks_config().timezone == "UTC"


def test_custom_prefix_keeps_the_shared_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alias is composed from the resolved prefix, not assumed."""
    # Arrange
    monkeypatch.setenv("GREL_TIMEZONE", "Europe/Zurich")
    monkeypatch.setenv("MYAPP_TIMEZONE", "America/Chicago")

    # Act
    config = resolve_config(
        TasksConfig,
        explicit=None,
        kwargs={},
        env_prefix="MYAPP_",
        shared_env=SHARED_TIMEZONE_ENV,
    )

    # Assert
    assert config.timezone == "America/Chicago"


def test_tasks_reads_the_shared_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Tasks` picks the app-wide timezone up end to end."""
    # Arrange
    monkeypatch.setenv("GREL_TIMEZONE", "Europe/Zurich")

    # Act / Assert
    assert Tasks().timezone == "Europe/Zurich"


def test_log_reads_the_shared_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same variable moves log timestamps."""
    # Arrange
    monkeypatch.setenv("GREL_TIMEZONE", "Europe/Zurich")

    # Act
    config = resolve_config(
        LogConfig,
        explicit=None,
        kwargs={},
        env_prefix="GREL_LOG_",
        shared_env=SHARED_TIMEZONE_ENV,
    )

    # Assert
    assert config.timezone == "Europe/Zurich"


def test_log_can_opt_back_out_to_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GREL_LOG_TIMEZONE` keeps logs on UTC under a local service."""
    # Arrange
    monkeypatch.setenv("GREL_TIMEZONE", "Europe/Zurich")
    monkeypatch.setenv("GREL_LOG_TIMEZONE", "UTC")

    # Act
    config = resolve_config(
        LogConfig,
        explicit=None,
        kwargs={},
        env_prefix="GREL_LOG_",
        shared_env=SHARED_TIMEZONE_ENV,
    )

    # Assert
    assert config.timezone == "UTC"


def test_shared_variable_is_reported_when_the_gate_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared variable set without the gate reports like any other."""
    # Arrange
    monkeypatch.delenv("GREL_ENV_LOAD", raising=False)
    monkeypatch.setenv("GREL_TIMEZONE", "Europe/Zurich")
    monkeypatch.setattr(
        "grelmicro._config._warned_ignored_env", set(), raising=True
    )

    # Act / Assert
    with pytest.warns(GrelmicroConfigWarning, match="GREL_TIMEZONE"):
        resolve_config(
            TasksConfig,
            explicit=None,
            kwargs={},
            env_prefix="GREL_TASK_",
            shared_env=SHARED_TIMEZONE_ENV,
        )


class TestWithoutTimezoneDatabase:
    """Behaviour on an image that carries no timezone files."""

    @pytest.fixture(autouse=True)
    def _no_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Report an empty timezone database."""
        monkeypatch.setattr(
            "grelmicro._timezone._known_timezones", dict, raising=True
        )

    def test_utc_still_resolves(self) -> None:
        """The default needs no timezone database."""
        # Act / Assert
        assert normalize_timezone_name("UTC") == "UTC"
        assert resolve_timezone("UTC").utcoffset(None) == timedelta(0)

    def test_other_names_name_the_missing_package(self) -> None:
        """The message says what to install rather than blaming the name."""
        # Act / Assert
        with pytest.raises(ValueError, match="tzdata"):
            normalize_timezone_name("Nope/Zone")

    def test_resolving_another_name_names_the_missing_package(self) -> None:
        """Resolution reports the missing database, not an invalid name."""
        # Act / Assert
        with pytest.raises(ValueError, match="tzdata"):
            resolve_timezone("Nope/Zone")

    def test_logging_configures_on_the_default(self) -> None:
        """The default log timezone needs no timezone database either.

        The failure this guards used to happen at import. Fixing the import
        moved it here, so the default has to be checked where it is used.
        """
        # Act
        loaded = load_settings(LogConfig())

        # Assert
        assert loaded.timezone.utcoffset(None) == timedelta(0)

    def test_a_loadable_name_is_taken_as_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Loading is the only remaining test, so a real zone still works."""
        # Arrange
        monkeypatch.setattr(
            "grelmicro._timezone.ZoneInfo", lambda name: name, raising=True
        )

        # Act / Assert
        assert normalize_timezone_name("Europe/Zurich") == "Europe/Zurich"


def test_empty_timezone_name_is_rejected() -> None:
    """A blank string names no zone."""
    # Act / Assert
    with pytest.raises(ValueError, match="empty"):
        normalize_timezone_name("   ")


def test_unknown_name_is_rejected_with_a_database_present() -> None:
    """A name absent from the database fails without mentioning tzdata."""
    # Act / Assert
    with pytest.raises(ValueError, match="unknown timezone name"):
        normalize_timezone_name("Nope/Zone")


class TestStartupOnlyReconfigure:
    """A field that only applies at startup reports rather than applies."""

    @pytest.fixture(autouse=True)
    def _reset_reports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Report again in each test, since reports are deduped per process."""
        monkeypatch.setattr(
            "grelmicro._config._warned_immutable_skipped", set(), raising=True
        )

    def test_changing_it_is_reported_and_skipped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An operator editing the timezone learns it needs a restart."""
        # Arrange
        current = TasksConfig(timezone="UTC", shutdown_timeout=30)

        # Act
        with caplog.at_level("WARNING", logger="grelmicro"):
            patched = resolve_config_from_mapping(
                current,
                env_prefix="GREL_TASK_",
                mapping={
                    "GREL_TASK_TIMEZONE": "Europe/Zurich",
                    "GREL_TASK_SHUTDOWN_TIMEOUT": "5",
                },
                immutable_fields=frozenset({"timezone"}),
            )

        # Assert
        assert patched.timezone == "UTC"
        assert patched.shutdown_timeout == SHUTDOWN_TIMEOUT
        assert "GREL_TASK_TIMEZONE" in caplog.text

    def test_the_running_value_is_not_reported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The mounted source repeats the startup value on every resync."""
        # Arrange
        current = TasksConfig(timezone="Europe/Zurich")

        # Act
        with caplog.at_level("WARNING", logger="grelmicro"):
            resolve_config_from_mapping(
                current,
                env_prefix="GREL_TASK_",
                mapping={"GREL_TASK_TIMEZONE": "Europe/Zurich"},
                immutable_fields=frozenset({"timezone"}),
            )

        # Assert
        assert caplog.text == ""

    def test_an_unusable_value_is_reported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A value the model rejects cannot be the one already running."""
        # Arrange
        current = TasksConfig(timezone="UTC")

        # Act
        with caplog.at_level("WARNING", logger="grelmicro"):
            resolve_config_from_mapping(
                current,
                env_prefix="GREL_TASK_",
                mapping={"GREL_TASK_TIMEZONE": "Nope/Zone"},
                immutable_fields=frozenset({"timezone"}),
            )

        # Assert
        assert "GREL_TASK_TIMEZONE" in caplog.text

    def test_a_key_naming_no_field_is_ignored(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An immutable name the config does not declare reports nothing."""
        # Arrange
        current = TasksConfig()

        # Act
        with caplog.at_level("WARNING", logger="grelmicro"):
            resolve_config_from_mapping(
                current,
                env_prefix="GREL_TASK_",
                mapping={"GREL_TASK_WORKER": "node-1"},
                immutable_fields=frozenset({"worker"}),
            )

        # Assert
        assert caplog.text == ""

    def test_it_is_reported_once_per_process(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Every resync carries the key again, so reporting is deduped."""
        # Arrange: a prefix of its own, so the process-wide dedupe set
        # cannot already hold this marker from another test.
        current = TasksConfig(timezone="UTC")
        mapping = {"GREL_DEDUPE_TIMEZONE": "Europe/Zurich"}

        # Act
        with caplog.at_level("WARNING", logger="grelmicro"):
            for _ in range(3):
                resolve_config_from_mapping(
                    current,
                    env_prefix="GREL_DEDUPE_",
                    mapping=mapping,
                    immutable_fields=frozenset({"timezone"}),
                )

        # Assert
        assert caplog.text.count("GREL_DEDUPE_TIMEZONE") == 1
