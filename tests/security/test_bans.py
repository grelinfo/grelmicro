"""Tests for banning clients that keep presenting tokens that do not verify.

This is a control that refuses traffic, so the cases that matter most are the
ones where it refuses the wrong traffic: a provider rotating its keys, a
client whose token merely expired, and an attacker trying to spend the
service's memory from an address range it has plenty of.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

import pytest
from pydantic import ValidationError

from grelmicro._config import reconfigure_all
from grelmicro.errors import AdmissionError, SettingsValidationError
from grelmicro.security import (
    ABUSIVE_REASONS,
    ClientBannedError,
    ClientBans,
    ClientBansConfig,
    TokenRejectedReason,
)

CLIENT = "203.0.113.9"
OTHER = "203.0.113.10"
FAILURES = 3
FLOOD = 5000
TRACKED = 64
RACE_THREADS = 8
RACE_CLIENTS = 256
RACE_TRACKED = 32
MIN_FAILURES = 5
MAX_BAN_SECONDS = 900
MIN_TRACKED = 1000
BAN = 60.0


def bans(**overrides: Any) -> ClientBans:  # noqa: ANN401
    """Return a ban table that trips quickly."""
    overrides.setdefault("failures", FAILURES)
    overrides.setdefault("window", 60.0)
    overrides.setdefault("duration", BAN)
    return ClientBans(**overrides)


class TestBanning:
    """Counting failures and refusing the client that produced them."""

    def test_an_unknown_client_is_not_banned(self) -> None:
        """Nothing is refused until it has earned it."""
        assert bans().banned(CLIENT) is False

    def test_failures_below_the_threshold_do_not_ban(self) -> None:
        """A client is allowed to get it wrong a few times."""
        table = bans()

        for _ in range(FAILURES - 1):
            assert table.record(CLIENT, "signature") is False

        assert table.banned(CLIENT) is False

    def test_the_threshold_bans(self) -> None:
        """The failure that reaches the threshold says so."""
        table = bans()
        for _ in range(FAILURES - 1):
            table.record(CLIENT, "signature")

        assert table.record(CLIENT, "signature") is True
        assert table.banned(CLIENT) is True

    def test_a_reason_member_counts_like_its_tag(self) -> None:
        """The reason a rejection carries is recorded as it is."""
        table = bans()

        for _ in range(FAILURES):
            table.record(CLIENT, TokenRejectedReason.SIGNATURE)

        assert table.banned(CLIENT) is True

    def test_a_ban_expires(self) -> None:
        """Addresses are shared, so a ban is a pause and not a verdict."""
        table = bans(duration=0.05)
        for _ in range(FAILURES):
            table.record(CLIENT, "signature")
        assert table.banned(CLIENT) is True

        time.sleep(0.1)

        assert table.banned(CLIENT) is False

    def test_one_client_is_banned_at_a_time(self) -> None:
        """A neighbour behind the same service is unaffected."""
        table = bans()
        for _ in range(FAILURES):
            table.record(CLIENT, "signature")

        assert table.banned(CLIENT) is True
        assert table.banned(OTHER) is False

    def test_failures_outside_the_window_start_over(self) -> None:
        """Occasional failures spread out are not a pattern."""
        table = bans(window=0.05)
        for _ in range(FAILURES - 1):
            table.record(CLIENT, "signature")

        time.sleep(0.1)

        assert table.record(CLIENT, "signature") is False
        assert table.banned(CLIENT) is False

    def test_the_window_rolling_over_does_not_lift_a_ban(self) -> None:
        """A client cannot serve out its ban by carrying on failing.

        The counting window is shorter than a ban, so a banned client that
        keeps sending forged tokens rolls its window over while still banned.
        Starting the count again must not take the ban with it.
        """
        table = bans(window=0.05, duration=30.0)
        for _ in range(FAILURES):
            table.record(CLIENT, "signature")
        assert table.banned(CLIENT) is True

        time.sleep(0.1)
        table.record(CLIENT, "signature")

        assert table.banned(CLIENT) is True

    def test_forget_clears_a_ban(self) -> None:
        """An operator can let a client back in."""
        table = bans()
        for _ in range(FAILURES):
            table.record(CLIENT, "signature")

        table.forget(CLIENT)

        assert table.banned(CLIENT) is False

    def test_forgetting_an_unknown_client_is_harmless(self) -> None:
        """Clearing what was never there is not an error."""
        bans().forget(CLIENT)

    def test_an_expired_ban_is_not_reported_again(self) -> None:
        """A failure after a ban ran out bans nobody until the count does."""
        table = bans(duration=0.05, window=0.05)
        for _ in range(FAILURES):
            table.record(CLIENT, "signature")

        time.sleep(0.1)

        assert table.record(CLIENT, "signature") is False
        assert table.banned(CLIENT) is False

    def test_a_client_recorded_after_forget_keeps_its_place(self) -> None:
        """The record `forget` left behind never evicts the new entry."""
        table = bans(max_clients=2)
        table.record(CLIENT, "signature")
        table.forget(CLIENT)
        table.record(CLIENT, "signature")

        table.record(OTHER, "signature")

        assert CLIENT in table._clients
        assert OTHER in table._clients


class TestRetryAfter:
    """How long a banned client is told to wait."""

    def test_a_banned_client_is_told_the_time_left(self) -> None:
        """The wait is what remains of the ban, never more."""
        table = bans()
        for _ in range(FAILURES):
            table.record(CLIENT, "signature")

        left = table.banned_for(CLIENT)

        assert 0.0 < left <= BAN

    def test_a_client_that_is_not_banned_waits_for_nothing(self) -> None:
        """An unknown client, or one still under the threshold, waits zero."""
        table = bans()
        table.record(OTHER, "signature")

        assert table.banned_for(CLIENT) == 0.0
        assert table.banned_for(OTHER) == 0.0

    def test_an_expired_ban_leaves_no_wait(self) -> None:
        """A ban that has run out is never reported as a negative delay."""
        table = bans(duration=0.05)
        for _ in range(FAILURES):
            table.record(CLIENT, "signature")

        time.sleep(0.1)

        assert table.banned_for(CLIENT) == 0.0

    def test_the_error_carries_the_wait(self) -> None:
        """It is an admission refusal, answered `429` with `Retry-After`."""
        error = ClientBannedError(retry_after=12.5)

        assert isinstance(error, AdmissionError)
        assert error.retry_after == 12.5  # noqa: PLR2004
        assert str(error) == "Too many rejected tokens from this client."


class TestWhatIsNotAbuse:
    """The failures that must never ban anyone.

    Counting these would turn an ordinary event into an outage: every client
    sees `unknown-key` for a moment when the provider rotates.
    """

    @pytest.mark.parametrize(
        "reason",
        [
            "unknown-key",
            "expired",
            "not-yet-valid",
            "audience",
            "issuer",
            "malformed",
            "revoked",
        ],
    )
    def test_an_ordinary_rejection_never_bans(self, reason: str) -> None:
        """A rotation, a stale token, a clock, or an opaque token is not an attack."""
        table = bans()

        for _ in range(FLOOD):
            assert table.record(CLIENT, reason) is False

        assert table.banned(CLIENT) is False

    def test_an_ordinary_rejection_is_not_even_remembered(self) -> None:
        """It costs nothing to be a client whose token expired."""
        table = bans()

        table.record(CLIENT, "expired")

        assert table._clients == {}

    @pytest.mark.parametrize("reason", sorted(ABUSIVE_REASONS))
    def test_a_forged_token_does_ban(self, reason: str) -> None:
        """Each counted reason means the token was built to pass as trusted."""
        table = bans()

        for _ in range(FAILURES):
            table.record(CLIENT, reason)

        assert table.banned(CLIENT) is True

    def test_the_counted_reasons_can_be_chosen(self) -> None:
        """A service that wants expiry counted can say so."""
        table = ClientBans(
            failures=2,
            window=60.0,
            duration=60.0,
            reasons=frozenset({"expired"}),
        )

        table.record(CLIENT, "signature")
        assert table.banned(CLIENT) is False

        table.record(CLIENT, "expired")
        table.record(CLIENT, "expired")
        assert table.banned(CLIENT) is True

    def test_the_default_set_is_the_two_forgeries(self) -> None:
        """The default is the one that cannot cause an outage."""
        assert frozenset({"algorithm", "signature"}) == ABUSIVE_REASONS
        assert "unknown-key" not in ABUSIVE_REASONS
        assert "expired" not in ABUSIVE_REASONS
        assert "malformed" not in ABUSIVE_REASONS


class TestMemoryIsBounded:
    """An attacker has more addresses than the service has memory."""

    def test_failing_from_many_addresses_is_bounded(self) -> None:
        """An IPv6 allocation must not become a memory cost."""
        table = bans(max_clients=TRACKED)

        for index in range(FLOOD):
            table.record(f"2001:db8::{index:x}", "signature")

        assert len(table._clients) <= TRACKED

    def test_spreading_failures_across_addresses_earns_no_ban(self) -> None:
        """One failure per address is what a botnet does, and it buys nothing."""
        table = bans(max_clients=TRACKED)

        for index in range(FLOOD):
            assert table.record(f"2001:db8::{index:x}", "signature") is False

    def test_the_oldest_tracked_client_is_dropped_first(self) -> None:
        """Making room takes from the end nothing is being written to."""
        table = bans(max_clients=2)
        table.record("first", "signature")
        table.record("second", "signature")

        table.record("third", "signature")

        assert "first" not in table._clients
        assert "third" in table._clients

    def test_a_tracked_client_failing_again_evicts_nobody(self) -> None:
        """Failing from one tracked address never lifts another's ban."""
        table = bans(failures=1, max_clients=2)
        table.record("victim", "signature")
        table.record(CLIENT, "signature")

        table.record(CLIENT, "signature")

        assert table.banned("victim") is True

    def test_the_least_recently_recorded_client_is_dropped(self) -> None:
        """A client failing again moves to the back, whenever it first failed."""
        table = bans(failures=3, max_clients=3)
        table.record("early", "signature")
        table.record("second", "signature")
        table.record("third", "signature")
        table.record("early", "signature")
        table.record("early", "signature")

        table.record("newcomer", "signature")

        assert table.banned("early") is True
        assert "second" not in table._clients

    def test_forgotten_clients_never_evict_a_ban(self) -> None:
        """Only clients still tracked count toward `max_clients`."""
        table = bans(failures=1, max_clients=4)
        table.record("banned", "signature")
        for index in range(3):
            cleared = f"cleared-{index}"
            table.record(cleared, "signature")
            table.forget(cleared)

        table.record(CLIENT, "signature")

        assert table.banned("banned") is True


class TestConfiguration:
    """What the settings refuse, and the two ways to give them."""

    @pytest.mark.parametrize("setting", ["failures", "max_clients"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_counts_must_be_positive(self, setting: str, value: int) -> None:
        """Zero failures would ban every client on sight."""
        settings: dict[str, Any] = {setting: value}

        with pytest.raises(ValidationError):
            ClientBansConfig(**settings)

    @pytest.mark.parametrize("setting", ["window", "duration"])
    @pytest.mark.parametrize("value", [0, -1.0])
    def test_durations_must_be_positive(
        self, setting: str, value: float
    ) -> None:
        """A ban of no seconds is not a ban."""
        settings: dict[str, Any] = {setting: value}

        with pytest.raises(ValidationError):
            ClientBansConfig(**settings)

    def test_a_refused_keyword_raises_the_settings_error(self) -> None:
        """The keyword door raises the one error every setting raises."""
        with pytest.raises(SettingsValidationError):
            ClientBans(failures=0)

    def test_from_config_takes_the_config_as_it_is(self) -> None:
        """A config assembled elsewhere builds the same table."""
        table = ClientBans.from_config(
            ClientBansConfig(failures=1, window=60.0, duration=BAN),
            reasons=frozenset({"expired"}),
        )

        assert table.record(CLIENT, "expired") is True
        assert table.banned(CLIENT) is True

    def test_defaults_are_forgiving(self) -> None:
        """The defaults must not ban a client having a bad afternoon."""
        settings = ClientBansConfig()

        assert settings.failures >= MIN_FAILURES
        assert settings.duration <= MAX_BAN_SECONDS
        assert settings.max_clients >= MIN_TRACKED

    def test_it_works_with_no_configuration(self) -> None:
        """The zero-argument form is the one people will reach for."""
        assert ClientBans().banned(CLIENT) is False


class TestUnderThreads:
    """The table is shared across a thread pool, so it must never raise."""

    def test_concurrent_use_never_raises(self) -> None:
        """Reads never take the lock, and interleave with every write."""
        table = bans(max_clients=RACE_TRACKED, duration=0.01, window=0.01)
        escaped: list[str] = []
        stop = threading.Event()

        def hammer(offset: int) -> None:
            index = offset
            while not stop.is_set():
                client = f"2001:db8::{index % RACE_CLIENTS:x}"
                try:
                    table.banned(client)
                    table.banned_for(client)
                    table.record(client, "signature")
                    table.forget(client)
                except Exception as error:  # noqa: BLE001
                    escaped.append(type(error).__name__)
                    return
                index += 1

        workers = [
            threading.Thread(target=hammer, args=(i,))
            for i in range(RACE_THREADS)
        ]
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-9)
        try:
            for worker in workers:
                worker.start()
            time.sleep(0.8)
            stop.set()
            for worker in workers:
                worker.join(timeout=5)
        finally:
            sys.setswitchinterval(previous)

        assert escaped == []
        assert len(table._clients) <= RACE_TRACKED


class TestEnvironment:
    """Thresholds a deployment tunes, at startup and while running."""

    def test_the_environment_tunes_the_table(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A setting left out of the code is read from its variable."""
        monkeypatch.setenv("GREL_ENV_LOAD", "1")
        monkeypatch.setenv("GREL_CLIENTBANS_FAILURES", "2")

        table = ClientBans()
        table.record(CLIENT, "signature")

        assert table.record(CLIENT, "signature") is True

    def test_a_named_table_falls_back_to_the_kind_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ban costs capacity, so one variable may tune every table."""
        monkeypatch.setenv("GREL_ENV_LOAD", "1")
        monkeypatch.setenv("GREL_CLIENTBANS_FAILURES", "2")

        assert ClientBans(name="edge").config.failures == 2  # noqa: PLR2004

    async def test_a_mounted_file_retunes_a_running_table(self) -> None:
        """Every threshold is live, and the next failure is judged by it."""
        table = ClientBans(failures=FAILURES, name="live")

        await reconfigure_all(
            {
                "GREL_CLIENTBANS_LIVE_FAILURES": "1",
                "GREL_CLIENTBANS_LIVE_DURATION": "30",
            }
        )

        assert table.config.failures == 1
        assert table.record(CLIENT, "signature") is True
        assert 0.0 < table.banned_for(CLIENT) <= 30  # noqa: PLR2004

    def test_a_table_from_a_config_is_never_reloaded(self) -> None:
        """The declarative door is the whole truth, so no file reaches it."""
        assert ClientBans.from_config(ClientBansConfig())._env_prefix is None
