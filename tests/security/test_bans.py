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
from collections import deque
from typing import Any

import pytest
from pydantic import ValidationError

from grelmicro.errors import SettingsValidationError
from grelmicro.security import (
    ABUSIVE_REASONS,
    ClientBannedError,
    ClientBans,
    ClientBansConfig,
    JWTConfig,
    JWTKey,
    JWTVerifier,
    TokenRejectedError,
)
from tests.security.jwt_signing import Signer

SIGNER = Signer()
AUDIENCE = "grelmicro-api"
ISSUER = "https://auth.grel.info/"
HOUR = 3600

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


def bans(**overrides: Any) -> ClientBans:  # noqa: ANN401
    """Return a ban table that trips quickly."""
    overrides.setdefault("failures", FAILURES)
    overrides.setdefault("window", 60.0)
    overrides.setdefault("duration", 60.0)
    return ClientBans(ClientBansConfig(**overrides))


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


class TestWhatIsNotAbuse:
    """The failures that must never ban anyone.

    Counting these would turn an ordinary event into an outage: every client
    sees `unknown-key` for a moment when the provider rotates.
    """

    @pytest.mark.parametrize(
        "reason",
        ["unknown-key", "expired", "not-yet-valid", "audience", "issuer"],
    )
    def test_an_ordinary_rejection_never_bans(self, reason: str) -> None:
        """A rotation, a stale token, or a clock is not an attack."""
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
        """Each counted reason means the token was never issued by anyone."""
        table = bans()

        for _ in range(FAILURES):
            table.record(CLIENT, reason)

        assert table.banned(CLIENT) is True

    def test_the_counted_reasons_can_be_chosen(self) -> None:
        """A service that wants expiry counted can say so."""
        table = ClientBans(
            ClientBansConfig(failures=2, window=60.0, duration=60.0),
            reasons=frozenset({"expired"}),
        )

        table.record(CLIENT, "signature")
        assert table.banned(CLIENT) is False

        table.record(CLIENT, "expired")
        table.record(CLIENT, "expired")
        assert table.banned(CLIENT) is True

    def test_the_default_set_excludes_rotation_and_expiry(self) -> None:
        """The default is the one that cannot cause an outage."""
        assert "unknown-key" not in ABUSIVE_REASONS
        assert "expired" not in ABUSIVE_REASONS
        assert {"algorithm", "malformed", "signature"} == ABUSIVE_REASONS


class TestMemoryIsBounded:
    """An attacker has more addresses than the service has memory."""

    def test_failing_from_many_addresses_is_bounded(self) -> None:
        """An IPv6 allocation must not become a memory cost."""
        table = bans(max_clients=TRACKED)

        for index in range(FLOOD):
            table.record(f"2001:db8::{index:x}", "signature")

        assert len(table._clients) <= TRACKED
        assert len(table._order) <= TRACKED + 1

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


class TestConfiguration:
    """What the settings refuse."""

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
        """Reads and writes interleave without a lock between them."""
        table = bans(max_clients=RACE_TRACKED, duration=0.01, window=0.01)
        escaped: list[str] = []
        stop = threading.Event()

        def hammer(offset: int) -> None:
            index = offset
            while not stop.is_set():
                client = f"2001:db8::{index % RACE_CLIENTS:x}"
                try:
                    table.banned(client)
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

    def test_an_emptied_queue_stops_the_eviction_loop(self) -> None:
        """Another thread can drain the queue while this one is evicting."""

        class Drained(deque):  # type: ignore[type-arg]
            """A queue that is empty the moment it is read from."""

            def popleft(self) -> object:
                raise IndexError

        table = bans(max_clients=1)
        table.record(CLIENT, "signature")
        table._order = Drained(table._order)

        table.record(OTHER, "signature")

        assert len(table._clients) >= 1


def token(**claims: Any) -> str:  # noqa: ANN401
    """Return a token the suite's verifier accepts."""
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "sub": "user-1",
        "aud": AUDIENCE,
        "exp": now + HOUR,
    }
    payload.update(claims)
    return SIGNER.token(payload, algorithm="RS256")


def verifier(**overrides: Any) -> JWTVerifier:  # noqa: ANN401
    """Return a verifier that bans after `FAILURES` forged tokens."""
    return JWTVerifier(
        JWTConfig(
            keys=[JWTKey(algorithm="RS256", key=SIGNER.public_pem("RS256"))],
            audience=[AUDIENCE],
            issuer=[ISSUER],
        ),
        bans=ClientBans(
            ClientBansConfig(
                failures=FAILURES, window=60.0, duration=60.0, **overrides
            )
        ),
    )


class TestOptIn:
    """Bans are wired into a verifier, or they are not there at all."""

    def test_a_verifier_without_bans_is_unchanged(self) -> None:
        """The default path does no ban bookkeeping."""
        plain = JWTVerifier(
            JWTConfig(
                keys=[
                    JWTKey(algorithm="RS256", key=SIGNER.public_pem("RS256"))
                ],
                audience=[AUDIENCE],
                issuer=[ISSUER],
            )
        )

        assert plain.verify(token()).subject == "user-1"
        assert plain.verify_header(f"Bearer {token()}").subject == "user-1"

    def test_a_client_without_bans_needs_no_address(self) -> None:
        """`client=` is accepted and ignored when nothing counts it."""
        plain = JWTVerifier(
            JWTConfig(
                keys=[
                    JWTKey(algorithm="RS256", key=SIGNER.public_pem("RS256"))
                ],
                audience=[AUDIENCE],
            )
        )

        assert plain.verify(token(), client=CLIENT).subject == "user-1"

    def test_repeated_forgery_bans_the_client(self) -> None:
        """The whole point: a forger stops being verified."""
        subject = verifier()
        forged = token()[:-3] + "AAA"

        for _ in range(FAILURES):
            with pytest.raises(TokenRejectedError):
                subject.verify(forged, client=CLIENT)

        with pytest.raises(ClientBannedError):
            subject.verify(token(), client=CLIENT)

    def test_a_banned_client_is_refused_before_the_token_is_read(self) -> None:
        """Even a perfectly good token does not get it back in."""
        subject = verifier()
        forged = token()[:-3] + "AAA"
        for _ in range(FAILURES):
            with pytest.raises(TokenRejectedError):
                subject.verify(forged, client=CLIENT)

        with pytest.raises(ClientBannedError):
            subject.verify_header(f"Bearer {token()}", client=CLIENT)

    def test_other_clients_are_untouched(self) -> None:
        """One caller misbehaving does not close the service."""
        subject = verifier()
        forged = token()[:-3] + "AAA"
        for _ in range(FAILURES):
            with pytest.raises(TokenRejectedError):
                subject.verify(forged, client=CLIENT)

        assert subject.verify(token(), client=OTHER).subject == "user-1"

    def test_an_expired_token_never_bans(self) -> None:
        """A client that needs to refresh is not an attacker."""
        subject = verifier()
        stale = token(exp=int(time.time()) - HOUR)

        for _ in range(FAILURES * 5):
            with pytest.raises(TokenRejectedError):
                subject.verify(stale, client=CLIENT)

        assert subject.verify(token(), client=CLIENT).subject == "user-1"

    def test_bans_without_a_client_are_refused_loudly(self) -> None:
        """Configured protection that counts nothing must not look configured."""
        subject = verifier()

        with pytest.raises(SettingsValidationError, match="client="):
            subject.verify(token())

    def test_the_header_path_also_refuses_a_missing_client(self) -> None:
        """Both doors into the verifier insist on the same thing."""
        subject = verifier()

        with pytest.raises(SettingsValidationError, match="client="):
            subject.verify_header(f"Bearer {token()}")

    def test_an_empty_client_is_refused(self) -> None:
        """An address nobody vouched for is not an address."""
        subject = verifier()

        with pytest.raises(SettingsValidationError, match="client="):
            subject.verify(token(), client="")
