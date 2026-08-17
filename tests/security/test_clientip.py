"""Tests for trusted-proxy client IP resolution.

The adversarial cases are the point. Each one corresponds to a published
spoofing advisory or to a parsing bug found in a widely used server.
"""

from __future__ import annotations

import logging
from ipaddress import ip_address, ip_network
from typing import TYPE_CHECKING, Any

import pytest

from grelmicro.errors import SettingsValidationError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]

from grelmicro.security import (
    ClientAddressMiddleware,
    ClientAddressReason,
    TrustedProxies,
    resolve_client_address,
)

TRUSTED = ["10.0.0.0/8"]
EXPECTED_HOPS = 2
EXPECTED_WARNED_PEERS = 8
PEER = "10.0.0.5"


def scope(
    peer: str | None = PEER,
    forwarded: str | list[str] | None = None,
    *,
    scope_type: str = "http",
    port: int = 1234,
) -> dict[str, Any]:
    """Build an ASGI scope with the given peer and forwarded header."""
    values = [forwarded] if isinstance(forwarded, str) else (forwarded or [])
    headers = [(b"x-forwarded-for", value.encode()) for value in values]
    built: dict[str, Any] = {"type": scope_type, "headers": headers}
    if peer is not None:
        built["client"] = (peer, port)
    return built


def resolve(
    peer: str | None = PEER,
    forwarded: str | list[str] | None = None,
    trusted: list[str] | None = None,
    **kwargs: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Resolve against a trusted set built from `trusted`."""
    return resolve_client_address(
        scope(peer, forwarded),
        TrustedProxies(TRUSTED if trusted is None else trusted, **kwargs),
    )


class TestSpoofing:
    """Every case here is a published vulnerability class."""

    def test_untrusted_peer_cannot_forge(self) -> None:
        """A direct caller's header is ignored entirely."""
        # Arrange / Act
        result = resolve(peer="9.9.9.9", forwarded="1.2.3.4")
        # Assert
        assert result.key == "9.9.9.9"
        assert result.reason is ClientAddressReason.UNTRUSTED_PEER

    def test_leftmost_entry_never_wins(self) -> None:
        """The client-supplied prefix loses to the proxy-written entry."""
        # Arrange / Act
        result = resolve(forwarded="9.9.9.9, 1.2.3.4")
        # Assert
        assert result.key == "1.2.3.4"
        assert result.reason is ClientAddressReason.RESOLVED

    def test_all_trusted_chain_returns_the_peer(self) -> None:
        """No proxy vouched for the leftmost, so it must not be used."""
        # Arrange / Act
        result = resolve(forwarded="10.1.1.1, 10.2.2.2")
        # Assert
        assert result.key == PEER
        assert result.reason is ClientAddressReason.CHAIN_EXHAUSTED

    def test_padding_stops_the_walk(self) -> None:
        """Injected garbage must not shift which entry is chosen."""
        # Arrange / Act
        result = resolve(forwarded="1.2.3.4, garbage, 10.0.0.6")
        # Assert
        assert result.key == PEER
        assert result.reason is ClientAddressReason.MALFORMED_ENTRY

    def test_non_ip_text_never_reaches_the_caller(self) -> None:
        """A rate limiter key can never become attacker-chosen text."""
        # Arrange / Act
        result = resolve(forwarded="<script>alert(1)</script>")
        # Assert
        assert result.key == PEER
        assert result.reason is ClientAddressReason.MALFORMED_ENTRY

    def test_wildcard_trust_still_refuses_the_leftmost(self) -> None:
        """Even trusting everything does not make the leftmost usable."""
        # Arrange / Act
        result = resolve(
            forwarded="1.2.3.4, 5.6.7.8", trusted=["0.0.0.0/0", "::/0"]
        )
        # Assert
        assert result.key == PEER
        assert result.reason is ClientAddressReason.CHAIN_EXHAUSTED


class TestResolution:
    """The ordinary paths."""

    def test_single_entry(self) -> None:
        """One entry from a trusted peer is the client."""
        # Arrange / Act
        result = resolve(forwarded="1.2.3.4")
        # Assert
        assert result.key == "1.2.3.4"
        assert result.hops == 0
        assert result.forwarded is True

    def test_counts_hops_past_trusted_proxies(self) -> None:
        """Each trusted proxy skipped is counted."""
        # Arrange / Act
        result = resolve(forwarded="1.2.3.4, 10.0.0.5, 10.0.0.6")
        # Assert
        assert result.key == "1.2.3.4"
        assert result.hops == EXPECTED_HOPS

    def test_multiple_header_instances_join_in_order(self) -> None:
        """Repeated header lines behave as one comma-joined line."""
        # Arrange / Act
        split = resolve(forwarded=["1.2.3.4", "10.0.0.6"])
        joined = resolve(forwarded="1.2.3.4, 10.0.0.6")
        # Assert
        assert split == joined
        assert split.key == "1.2.3.4"

    def test_no_header_returns_the_peer(self) -> None:
        """A trusted peer with no header is the client itself."""
        # Arrange / Act
        result = resolve()
        # Assert
        assert result.key == PEER
        assert result.reason is ClientAddressReason.NO_FORWARDED_HEADER
        assert result.forwarded is False

    def test_missing_peer_is_unresolvable(self) -> None:
        """No address is invented when the transport peer is absent."""
        # Arrange / Act
        result = resolve(peer=None)
        # Assert
        assert result is None

    def test_unparseable_peer_is_unresolvable(self) -> None:
        """A peer that is not an address resolves to nothing."""
        # Arrange / Act
        result = resolve(peer="not-an-ip")
        # Assert
        assert result is None

    def test_empty_trusted_set_always_returns_the_peer(self) -> None:
        """Trusting nothing is legal and means the header is never read."""
        # Arrange / Act
        result = resolve(forwarded="1.2.3.4", trusted=[])
        # Assert
        assert result.key == PEER
        assert result.reason is ClientAddressReason.UNTRUSTED_PEER


class TestParsing:
    """Address and port forms seen in real deployments."""

    @pytest.mark.parametrize(
        ("entry", "expected", "port"),
        [
            ("[2001:db8::1]:1234", "2001:db8::1", 1234),
            ("[2001:db8::1]", "2001:db8::1", None),
            ("2001:db8::1", "2001:db8::1", None),
            ("2001:db8::1:8080", "2001:db8::1:8080", None),
            ("1.2.3.4:5678", "1.2.3.4", 5678),
            ("1.2.3.4", "1.2.3.4", None),
        ],
    )
    def test_valid_forms(
        self, entry: str, expected: str, port: int | None
    ) -> None:
        """A bare IPv6 is never split on its last colon."""
        # Arrange / Act
        result = resolve(forwarded=entry)
        # Assert
        assert result.key == expected
        assert result.port == port

    @pytest.mark.parametrize(
        "entry",
        [
            "[2001:db8::1",
            "[2001:db8::1]x",
            "[2001:db8::1]:abc",
            "1.2.3.4:99999",
            "1",
            "010.1.1.1",
            "127.1",
            "0x7f.0.0.1",
            "10.0.0.0/8",
        ],
    )
    def test_rejected_forms(self, entry: str) -> None:
        """Lenient parsing is what turns `1` into `0.0.0.1` elsewhere."""
        # Arrange / Act
        result = resolve(forwarded=entry)
        # Assert
        assert result.reason is ClientAddressReason.MALFORMED_ENTRY

    @pytest.mark.parametrize("header", ["1.2.3.4, ", ", ,1.2.3.4", " 1.2.3.4 "])
    def test_empty_elements_are_dropped(self, header: str) -> None:
        """A trailing comma defeats resolution in some servers."""
        # Arrange / Act
        result = resolve(forwarded=header)
        # Assert
        assert result.key == "1.2.3.4"

    def test_unrelated_headers_are_skipped(self) -> None:
        """Other headers do not interfere with the scan."""
        # Arrange
        built = scope(forwarded="1.2.3.4")
        built["headers"] = [
            (b"user-agent", b"probe"),
            *built["headers"],
            (b"accept", b"*/*"),
        ]
        # Act
        result = resolve_client_address(built, TrustedProxies(TRUSTED))
        # Assert
        assert result is not None
        assert result.key == "1.2.3.4"

    def test_empty_header_is_no_header(self) -> None:
        """An empty value is not a malformed entry."""
        # Arrange / Act
        result = resolve(forwarded="")
        # Assert
        assert result.reason is ClientAddressReason.NO_FORWARDED_HEADER


class TestDualStack:
    """IPv4-mapped addresses must not split one caller into two buckets."""

    def test_mapped_peer_matches_an_ipv4_network(self) -> None:
        """A dual-stack listener still trusts the same proxy."""
        # Arrange / Act
        result = resolve(peer="::ffff:10.0.0.5", forwarded="1.2.3.4")
        # Assert
        assert result.key == "1.2.3.4"

    def test_mapped_entry_folds_onto_its_ipv4_key(self) -> None:
        """The same caller gets one rate limiter bucket, not two."""
        # Arrange / Act
        mapped = resolve(forwarded="::ffff:1.2.3.4")
        plain = resolve(forwarded="1.2.3.4")
        # Assert
        assert mapped.key == plain.key == "1.2.3.4"

    def test_mapped_network_trusts_a_plain_peer(self) -> None:
        """Normalisation works in both directions."""
        # Arrange / Act
        result = resolve(forwarded="1.2.3.4", trusted=["::ffff:10.0.0.0/104"])
        # Assert
        assert result.key == "1.2.3.4"

    def test_zone_id_is_dropped_from_the_key(self) -> None:
        """A scoped address is still comparable and keyable."""
        # Arrange / Act
        trusted = TrustedProxies(["fe80::/10"])
        # Assert
        assert ip_address("fe80::1%eth0") in trusted


class TestLimits:
    """Bounds that keep a hostile header from costing anything."""

    def test_long_chain_keeps_the_trusted_end(self) -> None:
        """The rightmost entries are the ones our own proxies wrote."""
        # Arrange
        header = ", ".join(["10.0.0.1"] * 200)
        # Act
        result = resolve(forwarded=header, max_entries=64)
        # Assert
        assert result.key == PEER
        assert result.reason is ClientAddressReason.TOO_MANY_ENTRIES

    def test_truncated_chain_is_not_reported_as_exhausted(self) -> None:
        """The entries past the cap were never read, so nothing exhausted."""
        # Arrange
        untruncated = ", ".join(["10.0.0.1"] * 64)
        truncated = ", ".join(["10.0.0.1"] * 65)
        # Act
        within = resolve(forwarded=untruncated, max_entries=64)
        beyond = resolve(forwarded=truncated, max_entries=64)
        # Assert
        assert within.reason is ClientAddressReason.CHAIN_EXHAUSTED
        assert beyond.reason is ClientAddressReason.TOO_MANY_ENTRIES

    def test_padding_cannot_force_the_proxy_bucket(self) -> None:
        """Prepended padding must not discard the real entry.

        Rejecting the whole header would key every padded request on the
        proxy, letting an attacker both evade a per-IP limit and drain the
        bucket every other caller behind that proxy shares.
        """
        # Arrange
        header = ", ".join(["9.9.9.9"] * 200 + ["8.8.8.8", "10.0.0.6"])
        # Act
        result = resolve(forwarded=header, max_entries=64)
        # Assert
        assert result.key == "8.8.8.8"
        assert result.reason is ClientAddressReason.RESOLVED

    def test_header_too_large(self) -> None:
        """An oversized header is refused before any entry is parsed."""
        # Arrange / Act
        result = resolve(forwarded="1.2.3.4" * 5000, max_header_bytes=1024)
        # Assert
        assert result.reason is ClientAddressReason.HEADER_TOO_LARGE

    def test_hop_limit_bounds_a_forged_chain(self) -> None:
        """A cap stops the walk once too many proxies have been passed."""
        # Arrange
        header = ", ".join(["10.0.0.1"] * 10)
        # Act
        result = resolve(forwarded=header, max_hops=2)
        # Assert
        assert result.reason is ClientAddressReason.HOP_LIMIT

    def test_hop_limit_permits_exactly_that_many(self) -> None:
        """`max_hops=2` allows a real two-proxy chain to resolve."""
        # Arrange / Act
        result = resolve(forwarded="1.2.3.4, 10.0.0.6, 10.0.0.7", max_hops=2)
        # Assert
        assert result.key == "1.2.3.4"
        assert result.reason is ClientAddressReason.RESOLVED


class TestTrustedProxiesConfig:
    """A typo must fail at startup, not silently match nothing."""

    @pytest.mark.parametrize(
        "entry", ["10.0.0.1/8", "not-an-ip", "*", "/unix/socket", ""]
    )
    def test_rejects_bad_entries(self, entry: str) -> None:
        """Uvicorn and granian downgrade these to dead string literals."""
        # Act / Assert
        with pytest.raises(ValueError, match="not an IP address"):
            TrustedProxies([entry])

    def test_accepts_parsed_objects(self) -> None:
        """Addresses and networks can be passed already parsed."""
        # Arrange / Act
        trusted = TrustedProxies(
            [ip_network("10.0.0.0/8"), ip_address("192.168.1.1")]
        )
        # Assert
        assert ip_address("10.1.2.3") in trusted
        assert ip_address("192.168.1.1") in trusted
        assert ip_address("8.8.8.8") not in trusted

    def test_error_names_the_entry_and_the_alternative(self) -> None:
        """The message says what to pass instead of a wildcard."""
        # Act / Assert
        with pytest.raises(ValueError, match=r"0\.0\.0\.0/0"):
            TrustedProxies(["*"])


async def _receive() -> Message:
    """Return a request message. Never called by these tests."""
    return {"type": "http.request"}  # pragma: no cover


async def _send(message: Message) -> None:
    """Discard the message. Never called by these tests."""


class TestMiddleware:
    """The caching wrapper."""

    async def test_caches_the_resolved_address(self) -> None:
        """Every consumer reads one value instead of resolving its own."""
        # Arrange
        seen: dict[str, Any] = {}

        async def app(
            scope: Scope,
            receive: Receive,  # noqa: ARG001
            send: Send,  # noqa: ARG001
        ) -> None:
            seen.update(scope["state"])

        middleware = ClientAddressMiddleware(
            app, trusted=TrustedProxies(TRUSTED)
        )
        # Act
        await middleware(scope(forwarded="1.2.3.4"), _receive, _send)
        # Assert
        assert seen["client_address"].key == "1.2.3.4"

    async def test_passes_other_scopes_through(self) -> None:
        """A lifespan scope is never touched."""
        # Arrange
        seen: list[str] = []

        async def app(
            scope: Scope,
            receive: Receive,  # noqa: ARG001
            send: Send,  # noqa: ARG001
        ) -> None:
            seen.append(scope["type"])

        middleware = ClientAddressMiddleware(
            app, trusted=TrustedProxies(TRUSTED)
        )
        # Act
        await middleware({"type": "lifespan"}, _receive, _send)
        # Assert
        assert seen == ["lifespan"]

    async def test_overwrite_scope_client_is_opt_in(self) -> None:
        """The verified peer is kept unless the shim is asked for."""
        # Arrange
        captured: dict[str, Any] = {}

        async def app(
            scope: Scope,
            receive: Receive,  # noqa: ARG001
            send: Send,  # noqa: ARG001
        ) -> None:
            captured["client"] = scope["client"]

        default = ClientAddressMiddleware(app, trusted=TrustedProxies(TRUSTED))
        shim = ClientAddressMiddleware(
            app, trusted=TrustedProxies(TRUSTED), overwrite_scope_client=True
        )
        # Act
        await default(scope(forwarded="1.2.3.4"), _receive, _send)
        untouched = captured["client"]
        await shim(scope(forwarded="1.2.3.4"), _receive, _send)
        # Assert
        assert untouched == (PEER, 1234)
        assert captured["client"] == ("1.2.3.4", 0)

    async def test_unresolvable_peer_caches_nothing(self) -> None:
        """No address is invented when there is no peer."""
        # Arrange
        seen: dict[str, Any] = {}

        async def app(
            scope: Scope,
            receive: Receive,  # noqa: ARG001
            send: Send,  # noqa: ARG001
        ) -> None:
            seen.update(scope.get("state", {}))

        middleware = ClientAddressMiddleware(
            app, trusted=TrustedProxies(TRUSTED)
        )
        # Act
        await middleware(scope(peer=None), _receive, _send)
        # Assert
        assert "client_address" not in seen


class TestHostilePorts:
    """A port that is digit-like but not an integer used to crash."""

    @pytest.mark.parametrize(
        "entry",
        ["1.2.3.4:\xb2", "1.2.3.4:8\xb2", "[2001:db8::1]:\xb9"],
    )
    def test_superscript_port_does_not_raise(self, entry: str) -> None:
        """`str.isdigit()` is true for these, but `int()` rejects them."""
        # Arrange / Act
        result = resolve(forwarded=entry)
        # Assert
        assert result.reason is ClientAddressReason.MALFORMED_ENTRY


class TestDegraded:
    """`degraded` marks a fallback, so callers need not list reasons."""

    @pytest.mark.parametrize(
        ("forwarded", "expected"),
        [
            ("1.2.3.4", False),
            (None, False),
            ("10.1.1.1, 10.2.2.2", True),
            ("1.2.3.4, junk, 10.0.0.6", True),
        ],
    )
    def test_flags_fallbacks(
        self,
        forwarded: str | None,
        expected: bool,  # noqa: FBT001
    ) -> None:
        """A fallback holds the proxy address, not the caller's."""
        # Arrange / Act
        result = resolve(forwarded=forwarded)
        # Assert
        assert result.degraded is expected

    def test_untrusted_peer_is_not_degraded(self) -> None:
        """A direct caller's own address is the caller's address."""
        # Arrange / Act
        result = resolve(peer="9.9.9.9")
        # Assert
        assert result.degraded is False

    def test_a_truncated_chain_is_degraded(self) -> None:
        """A bound leaves the proxy's own address, whichever bound it is."""
        # Arrange
        header = ", ".join(["10.0.0.1"] * 65)
        # Act
        result = resolve(forwarded=header, max_entries=64)
        # Assert
        assert result.reason is ClientAddressReason.TOO_MANY_ENTRIES
        assert result.degraded is True


class TestForwarded:
    """`forwarded` is the predicate an identity check uses."""

    @pytest.mark.parametrize(
        ("peer", "forwarded", "expected"),
        [
            (PEER, "1.2.3.4", True),
            (PEER, None, False),
            (PEER, "10.1.1.1, 10.2.2.2", False),
            ("9.9.9.9", "1.2.3.4", False),
        ],
    )
    def test_only_a_vouched_address_is_forwarded(
        self,
        peer: str,
        forwarded: str | None,
        expected: bool,  # noqa: FBT001
    ) -> None:
        """Only `RESOLVED` means a trusted proxy wrote the entry."""
        # Arrange / Act
        result = resolve(peer=peer, forwarded=forwarded)
        # Assert
        assert result.forwarded is expected

    def test_a_mistyped_trusted_set_fails_closed(self) -> None:
        """The bypass in #636: the guard must refuse, not admit the proxy."""
        # Arrange / Act
        result = resolve(
            peer="10.0.0.5", forwarded="1.2.3.4", trusted=["10.1.0.0/16"]
        )
        # Assert
        assert result.key == "10.0.0.5"
        assert result.degraded is False
        assert result.forwarded is False


class TestUntrustedPeerWarning:
    """A proxy missing from the trusted set is otherwise silent."""

    def test_warns_once_per_peer(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One line names the peer, and a flood cannot follow it."""
        # Arrange
        trusted = TrustedProxies(TRUSTED)
        request = scope(peer="9.9.9.9", forwarded="1.2.3.4")
        # Act
        with caplog.at_level(
            logging.WARNING, logger="grelmicro.security.clientip"
        ):
            result = resolve_client_address(request, trusted)
            resolve_client_address(request, trusted)
        # Assert
        assert result is not None
        assert result.reason is ClientAddressReason.UNTRUSTED_PEER
        assert caplog.text.count("Ignored X-Forwarded-For") == 1
        assert "9.9.9.9" in caplog.text

    def test_a_prober_cannot_mask_a_mistyped_trusted_set(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The report a forgotten proxy needs is not one a caller can take."""
        # Arrange
        trusted = TrustedProxies(TRUSTED)
        # Act
        with caplog.at_level(
            logging.WARNING, logger="grelmicro.security.clientip"
        ):
            for last in range(1, 5):
                probe = scope(peer=f"203.0.113.{last}", forwarded="1.2.3.4")
                resolve_client_address(probe, trusted)
            forgotten = scope(peer="192.168.1.10", forwarded="1.2.3.4")
            resolve_client_address(forgotten, trusted)
        # Assert
        assert "192.168.1.10" in caplog.text

    def test_a_flood_of_peers_goes_quiet(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Remembering every peer that ever probed would be the real leak."""
        # Arrange
        trusted = TrustedProxies(TRUSTED)
        # Act
        with caplog.at_level(
            logging.WARNING, logger="grelmicro.security.clientip"
        ):
            for last in range(1, 40):
                probe = scope(peer=f"203.0.113.{last}", forwarded="1.2.3.4")
                resolve_client_address(probe, trusted)
        # Assert
        assert (
            caplog.text.count("Ignored X-Forwarded-For")
            == EXPECTED_WARNED_PEERS
        )

    @pytest.mark.parametrize("forwarded", [None, "", "   "])
    def test_an_empty_header_is_not_a_forwarding_attempt(
        self, forwarded: str | None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Direct health checks must not spend another peer's report."""
        # Arrange
        trusted = TrustedProxies(TRUSTED)
        # Act
        with caplog.at_level(
            logging.WARNING, logger="grelmicro.security.clientip"
        ):
            resolve_client_address(
                scope(peer="9.9.9.9", forwarded=forwarded), trusted
            )
            silent = caplog.text
            resolve_client_address(
                scope(peer="9.9.9.9", forwarded="1.2.3.4"), trusted
            )
        # Assert
        assert silent == ""
        assert caplog.text.count("Ignored X-Forwarded-For") == 1

    def test_an_empty_trusted_set_stays_silent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Trusting nothing is a deployment, not a misconfiguration."""
        # Arrange
        trusted = TrustedProxies([])
        # Act
        with caplog.at_level(
            logging.WARNING, logger="grelmicro.security.clientip"
        ):
            resolve_client_address(
                scope(peer="9.9.9.9", forwarded="1.2.3.4"), trusted
            )
        # Assert
        assert caplog.text == ""

    def test_a_trusted_peer_stays_silent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The header was believed, so there is nothing to report."""
        # Arrange
        trusted = TrustedProxies(TRUSTED)
        # Act
        with caplog.at_level(
            logging.WARNING, logger="grelmicro.security.clientip"
        ):
            resolve_client_address(
                scope(peer=PEER, forwarded="1.2.3.4"), trusted
            )
        # Assert
        assert caplog.text == ""


class TestConfigTypes:
    """The trusted set rejects what the docs say it rejects."""

    @pytest.mark.parametrize("entry", [16909060, b"\x01\x02\x03\x04", None])
    def test_rejects_non_string_scalars(self, entry: object) -> None:
        """`ip_network` accepts ints and bytes, which is not what we mean."""
        # Act / Assert
        with pytest.raises(SettingsValidationError, match="Pass a string"):
            TrustedProxies([entry])  # ty: ignore[invalid-argument-type]


class TestBoundValidation:
    """A bound that caps nothing is refused at construction."""

    @pytest.mark.parametrize("value", [0, -1])
    def test_max_entries_must_be_positive(self, value: int) -> None:
        """`max_entries=0` slices as `entries[-0:]`, which is the whole list.

        The cap reads as "allow nothing" and silently allows everything, so
        it is rejected rather than accepted and ignored.
        """
        with pytest.raises(ValueError, match=r"max_entries must be > 0"):
            TrustedProxies(["10.0.0.0/8"], max_entries=value)

    @pytest.mark.parametrize("value", [0, -1])
    def test_max_header_bytes_must_be_positive(self, value: int) -> None:
        """Same reasoning as `max_entries`: a zero cap caps nothing."""
        with pytest.raises(ValueError, match=r"max_header_bytes must be > 0"):
            TrustedProxies(["10.0.0.0/8"], max_header_bytes=value)

    def test_max_hops_may_be_zero_but_not_negative(self) -> None:
        """`max_hops` counts proxies to walk past, so zero is meaningful."""
        assert TrustedProxies(["10.0.0.0/8"], max_hops=0) is not None

        with pytest.raises(ValueError, match=r"max_hops must be >= 0"):
            TrustedProxies(["10.0.0.0/8"], max_hops=-1)
