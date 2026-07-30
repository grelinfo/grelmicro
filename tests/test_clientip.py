"""Tests for trusted-proxy client IP resolution.

The adversarial cases are the point. Each one corresponds to a published
spoofing advisory or to a parsing bug found in a widely used server.
"""

from __future__ import annotations

from ipaddress import ip_address, ip_network
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]

from grelmicro.clientip import (
    ClientAddressMiddleware,
    ClientAddressReason,
    TrustedProxies,
    resolve_client_address,
)

TRUSTED = ["10.0.0.0/8"]
EXPECTED_HOPS = 2
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
        assert result.reason is ClientAddressReason.CHAIN_EXHAUSTED

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


class TestConfigTypes:
    """The trusted set rejects what the docs say it rejects."""

    @pytest.mark.parametrize("entry", [16909060, b"\x01\x02\x03\x04", None])
    def test_rejects_non_string_scalars(self, entry: object) -> None:
        """`ip_network` accepts ints and bytes, which is not what we mean."""
        # Act / Assert
        with pytest.raises(TypeError, match="Pass a string"):
            TrustedProxies([entry])  # ty: ignore[invalid-argument-type]
