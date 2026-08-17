"""Client IP resolution behind trusted proxies.

`X-Forwarded-For` is append-only, so its leftmost entry is whatever the
original caller claimed. Reading it is the flaw behind a long line of
spoofing advisories. This module resolves the address a trusted proxy
actually vouched for, and never returns anything else.

Read more in the [Client IP](../security/clientip.md) docs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)
from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

from grelmicro.errors import SettingsValidationError

if TYPE_CHECKING:
    from collections.abc import (
        Awaitable,
        Callable,
        Iterable,
        MutableMapping,
        Sequence,
    )

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

    IPAddress = IPv4Address | IPv6Address
    IPNetwork = IPv4Network | IPv6Network

__all__ = [
    "ClientAddress",
    "ClientAddressMiddleware",
    "ClientAddressReason",
    "TrustedProxies",
    "resolve_client_address",
]

logger = logging.getLogger("grelmicro.security.clientip")

_FORWARDED_FOR = b"x-forwarded-for"

_MAX_PORT = 65535

_IPV4_MAPPED_PREFIX = 96
"""Prefix length at which an IPv6 network is wholly IPv4-mapped space."""

_MAX_WARNED_PEERS = 8
"""Distinct untrusted peers reported before the warning goes quiet."""


class ClientAddressReason(StrEnum):
    """Why `ClientAddress.ip` holds the address it holds.

    `RESOLVED` is the one value the library can vouch for on its own: a
    trusted proxy wrote the entry. Every other value means `ip` is the
    verified transport peer, and they split in two.

    With `NO_FORWARDED_HEADER` and `UNTRUSTED_PEER` the peer is the
    caller, as long as nothing sits in front of the app that is missing
    from the trusted set. That is a claim about your topology, not one
    this module can check.

    Every remaining value means the peer is itself a trusted proxy whose
    chain could not be walked to a client, so `ip` is one of your own
    proxies and never the caller. `ClientAddress.degraded` marks exactly
    those.
    """

    RESOLVED = "resolved"
    """A trusted proxy vouched for this address."""

    NO_FORWARDED_HEADER = "no-forwarded-header"
    """A trusted peer sent no `X-Forwarded-For`."""

    UNTRUSTED_PEER = "untrusted-peer"
    """The peer is not a trusted proxy, so the header was ignored."""

    CHAIN_EXHAUSTED = "chain-exhausted"
    """Every entry in the chain was a trusted proxy."""

    HOP_LIMIT = "hop-limit"
    """The walk passed more trusted proxies than `max_hops`."""

    MALFORMED_ENTRY = "malformed-entry"
    """An entry was not an address, which stops the walk."""

    HEADER_TOO_LARGE = "header-too-large"
    """The header was longer than `max_header_bytes`."""

    TOO_MANY_ENTRIES = "too-many-entries"
    """More entries than `max_entries`, and every one read was trusted."""


@dataclass(frozen=True, slots=True)
class ClientAddress:
    """A resolved client address, with how it was reached."""

    ip: Annotated[
        IPAddress,
        Doc("The address. Always parsed, never raw header text."),
    ]
    port: Annotated[int | None, Doc("The source port, when one was given.")]
    reason: Annotated[
        ClientAddressReason,
        Doc("Why this address was chosen. `RESOLVED` is the trusted case."),
    ]
    hops: Annotated[
        int,
        Doc("Trusted proxies skipped before landing on this address."),
    ]

    @property
    def forwarded(self) -> bool:
        """Whether a trusted proxy vouched for this address.

        The test to use behind a proxy for anything that treats the
        address as the caller's identity, such as an allowlist or a
        private network check. It is True for `RESOLVED` alone.
        """
        return self.reason is ClientAddressReason.RESOLVED

    @property
    def degraded(self) -> bool:
        """Whether `ip` is one of your own proxies rather than a caller.

        True when the peer was a trusted proxy but its chain could not be
        walked to a client, so `ip` is that proxy's own address and is
        never the caller's. Any check that treats it as the caller must
        refuse.

        False is not the same as vouched for. It also covers the two
        cases where the peer is the caller only if nothing unlisted sits
        in front of the app: `UNTRUSTED_PEER` and `NO_FORWARDED_HEADER`.
        Use `forwarded` when the address has to carry an identity behind
        a proxy.
        """
        return self.reason not in (
            ClientAddressReason.RESOLVED,
            ClientAddressReason.NO_FORWARDED_HEADER,
            ClientAddressReason.UNTRUSTED_PEER,
        )

    @property
    def key(self) -> str:
        """Canonical form, safe to use as a rate limiter key.

        An IPv4-mapped address folds onto its IPv4 form, so one caller
        never occupies two buckets.

        It is a bucket, not an identity. When `degraded` is True every
        caller behind the proxy shares one key, so anything that has to
        keep callers apart, such as an idempotency key or an allowlist,
        checks `forwarded` first.
        """
        return self.ip.compressed


def _canonical(address: IPAddress) -> IPAddress:
    """Fold an IPv4-mapped address to IPv4 and drop any zone id."""
    if isinstance(address, IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address.scope_id is not None:
            return IPv6Address(address.packed)
    return address


def _canonical_network(network: IPNetwork) -> IPNetwork:
    """Fold a wholly IPv4-mapped IPv6 network to its IPv4 equivalent."""
    if (
        isinstance(network, IPv6Network)
        and network.prefixlen >= _IPV4_MAPPED_PREFIX
        and network.network_address.ipv4_mapped is not None
    ):
        host = network.network_address.ipv4_mapped
        return IPv4Network(
            (host, network.prefixlen - _IPV4_MAPPED_PREFIX), strict=False
        )
    return network


class TrustedProxies:
    """The proxies whose `X-Forwarded-For` entries may be believed.

    Build one at startup and reuse it. Every entry is parsed strictly, so
    a typo fails here rather than becoming a rule that silently matches
    nothing.

    There is no wildcard. To trust every peer, pass `["0.0.0.0/0", "::/0"]`,
    which is visible in a configuration review in a way a `*` is not.
    """

    __slots__ = (
        "_max_entries",
        "_max_header_bytes",
        "_max_hops",
        "_networks",
        "_warned_peers",
    )

    def __init__(
        self,
        networks: Annotated[
            Iterable[str | IPAddress | IPNetwork],
            Doc(
                """
                Addresses and CIDR ranges of your own proxies.

                Required, with no default. An empty iterable is legal and
                means trust nothing, so resolution always returns the
                transport peer.
                """
            ),
        ],
        /,
        *,
        max_hops: Annotated[
            int | None,
            Doc(
                "Most trusted proxies to walk past. Bounds a forged chain. "
                "None means no cap."
            ),
        ] = None,
        max_entries: Annotated[
            int,
            Doc("Most header entries to parse before giving up."),
        ] = 64,
        max_header_bytes: Annotated[
            int,
            Doc("Most header bytes to parse before giving up."),
        ] = 8192,
    ) -> None:
        """Compile the trusted set, rejecting anything unparsable.

        Raises:
            ValueError: If a bound is outside its usable range. `max_entries`
                and `max_header_bytes` cap work, so zero or less caps nothing.
                `max_hops` counts proxies to walk past, so it may be zero but
                never negative.
        """
        _validate_positive("max_entries", max_entries)
        _validate_positive("max_header_bytes", max_header_bytes)
        if max_hops is not None and max_hops < 0:
            msg = "max_hops must be >= 0"
            raise SettingsValidationError(msg)
        self._networks = tuple(_compile_entry(entry) for entry in networks)
        self._max_hops = max_hops
        self._max_entries = max_entries
        self._max_header_bytes = max_header_bytes
        self._warned_peers: set[IPAddress] | None = (
            set() if self._networks else None
        )

    def __contains__(self, address: IPAddress) -> bool:
        """Whether `address` belongs to a trusted proxy."""
        canonical = _canonical(address)
        return any(canonical in network for network in self._networks)


def _validate_positive(name: str, value: int) -> None:
    """Reject a bound that caps nothing.

    A cap of zero reads as "allow nothing" but slices as `entries[-0:]`,
    which is the whole list, so it silently disables the truncation it was
    meant to enforce.

    Raises:
        ValueError: If `value` is not greater than zero.
    """
    if value <= 0:
        msg = f"{name} must be > 0"
        raise SettingsValidationError(msg)


def _compile_entry(entry: str | IPAddress | IPNetwork) -> IPNetwork:
    """Parse one trusted-set entry, or raise naming it."""
    if isinstance(entry, (IPv4Network, IPv6Network)):
        return _canonical_network(entry)
    if isinstance(entry, (IPv4Address, IPv6Address)):
        return _canonical_network(ip_network(_canonical(entry)))
    if not isinstance(entry, str):
        msg = (
            f"TrustedProxies got a {type(entry).__name__} entry. "
            f"Pass a string, an ip_address, or an ip_network."
        )
        raise SettingsValidationError(msg)
    try:
        return _canonical_network(ip_network(entry))
    except ValueError:
        msg = (
            "TrustedProxies got an entry that is not an IP address or "
            "CIDR range. There is no wildcard, pass "
            '["0.0.0.0/0", "::/0"] to trust every peer.'
        )
        raise SettingsValidationError(msg) from None


def _split_host_port(  # noqa: PLR0911
    value: str,
) -> tuple[str, int | None] | None:
    """Split a forwarded entry into host and port, or None if malformed.

    A bare IPv6 literal always carries at least two colons, so the count
    disambiguates it from `host:port` without splitting on the last colon.
    """
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        host = value[1:end]
        rest = value[end + 1 :]
        if not rest:
            return host, None
        if not rest.startswith(":"):
            return None
        return _with_port(host, rest[1:])
    colons = value.count(":")
    if colons > 1:
        return value, None
    if colons == 1:
        host, _, port = value.partition(":")
        return _with_port(host, port)
    return value, None


def _with_port(host: str, port: str) -> tuple[str, int | None] | None:
    """Attach a numeric port to `host`, or None when it is not one."""
    # `isdigit` alone is true for Latin-1 superscripts, which `int` rejects.
    if not (port.isascii() and port.isdigit()) or int(port) > _MAX_PORT:
        return None
    return host, int(port)


def _warn_untrusted_peer(
    trusted: TrustedProxies,
    warned: set[IPAddress],
    headers: Sequence[tuple[bytes, bytes]],
    peer: IPAddress,
) -> None:
    """Report an untrusted peer that sent a forwarded header, once.

    A peer sending nothing in the header is not making a forwarding
    attempt, and reporting it would spend the report a misconfigured
    proxy needs.
    """
    if not any(
        name.lower() == _FORWARDED_FOR and value.strip()
        for name, value in headers
    ):
        return
    warned.add(peer)
    if len(warned) >= _MAX_WARNED_PEERS:
        trusted._warned_peers = None  # noqa: SLF001
    logger.warning(
        "Ignored X-Forwarded-For from %s, which is not a trusted proxy. "
        "Add it to TrustedProxies if it is one of yours, otherwise a "
        "caller is sending the header directly. Reported once per peer.",
        peer.compressed,
    )


def _collect_header(
    headers: Sequence[tuple[bytes, bytes]], limit: int
) -> bytes | None:
    """Join every `X-Forwarded-For` value, or None past `limit` bytes."""
    parts: list[bytes] = []
    total = 0
    for name, value in headers:
        if name.lower() != _FORWARDED_FOR:
            continue
        total += len(value) + 1
        if total > limit:
            return None
        parts.append(value)
    if not parts:
        return b""
    return b",".join(parts)


def resolve_client_address(  # noqa: C901, PLR0911
    scope: Annotated[Scope, Doc("The ASGI scope of the request.")],
    trusted: Annotated[
        TrustedProxies,
        Doc("The proxies whose forwarded entries may be believed."),
    ],
) -> ClientAddress | None:
    """Resolve the client address a trusted proxy vouched for.

    Returns None when the transport peer is absent or unparsable, which
    a caller must handle rather than being handed a fabricated address.

    The header is read only when the peer itself is trusted, and the walk
    runs right to left, because the rightmost entries are the ones your
    own proxies wrote. The first entry that is not a trusted proxy is the
    client. A malformed entry stops the walk rather than being skipped,
    since skipping lets padding shift which entry is chosen.

    When every entry is trusted, the peer is returned with
    `CHAIN_EXHAUSTED` rather than the leftmost entry. No trusted proxy
    vouched for that entry against an untrusted peer, so it is exactly
    the value that must not become a rate limiter key. The same walk
    ending inside a header the `max_entries` cap truncated returns
    `TOO_MANY_ENTRIES`, since the entries beyond the cap were never read.

    An untrusted peer that sends a non-empty `X-Forwarded-For` is logged
    on the `grelmicro.security.clientip` logger, once per peer and for at most
    eight peers. A caller probing the header therefore cannot flood the
    log, nor take the report a misconfigured proxy of yours needs.
    Nothing is logged when the trusted set is empty, which is the
    deployment that means trust nothing.
    """
    client = scope.get("client")
    if not client:
        return None
    try:
        peer = _canonical(ip_address(client[0]))
    except ValueError:
        return None
    port = client[1] if len(client) > 1 else None

    def from_peer(reason: ClientAddressReason, hops: int = 0) -> ClientAddress:
        return ClientAddress(ip=peer, port=port, reason=reason, hops=hops)

    headers = scope.get("headers", ())
    if peer not in trusted:
        warned = trusted._warned_peers  # noqa: SLF001
        if warned is not None and peer not in warned:
            _warn_untrusted_peer(trusted, warned, headers, peer)
        return from_peer(ClientAddressReason.UNTRUSTED_PEER)

    raw = _collect_header(
        headers,
        trusted._max_header_bytes,  # noqa: SLF001
    )
    if raw is None:
        return from_peer(ClientAddressReason.HEADER_TOO_LARGE)
    entries = [part.strip() for part in raw.split(b",")]
    entries = [part for part in entries if part]
    if not entries:
        return from_peer(ClientAddressReason.NO_FORWARDED_HEADER)
    exhausted = ClientAddressReason.CHAIN_EXHAUSTED
    if len(entries) > trusted._max_entries:  # noqa: SLF001
        # Keep the rightmost entries, the ones our own proxies wrote.
        # Discarding the header instead would let prepended padding force
        # every caller onto the proxy's key. The entries dropped were never
        # read, so running out of them is not an exhausted chain.
        entries = entries[-trusted._max_entries :]  # noqa: SLF001
        exhausted = ClientAddressReason.TOO_MANY_ENTRIES

    hops = 0
    for entry in reversed(entries):
        split = _split_host_port(entry.decode("latin-1"))
        if split is None:
            return from_peer(ClientAddressReason.MALFORMED_ENTRY, hops)
        host, entry_port = split
        try:
            address = _canonical(ip_address(host))
        except ValueError:
            return from_peer(ClientAddressReason.MALFORMED_ENTRY, hops)
        if address not in trusted:
            return ClientAddress(
                ip=address,
                port=entry_port,
                reason=ClientAddressReason.RESOLVED,
                hops=hops,
            )
        hops += 1
        max_hops = trusted._max_hops  # noqa: SLF001
        if max_hops is not None and hops > max_hops:
            return from_peer(ClientAddressReason.HOP_LIMIT, hops)
    return from_peer(exhausted, hops)


class ClientAddressMiddleware:
    """Resolve the client address once and cache it on the request.

    Stores a `ClientAddress` under `scope["state"]["client_address"]`, so
    every handler and every other middleware reads one value instead of
    each resolving its own and drifting apart.

    ```python
    from grelmicro.security import ClientAddressMiddleware, TrustedProxies

    app.add_middleware(
        ClientAddressMiddleware, trusted=TrustedProxies(["10.0.0.0/8"])
    )
    ```

    Pure ASGI, so it works on any ASGI server. It acts on `http` and
    `websocket` scopes and passes every other scope through untouched.
    """

    def __init__(
        self,
        app: Annotated[ASGIApp, Doc("The next ASGI application in the chain.")],
        *,
        trusted: Annotated[
            TrustedProxies,
            Doc("The proxies whose forwarded entries may be believed."),
        ],
        overwrite_scope_client: Annotated[
            bool,
            Doc(
                """
                Also rewrite `scope["client"]` with the resolved address.

                Off by default, because it discards the verified transport
                peer that an audit record wants, and because a server that
                already rewrites it would then be running the walk twice.
                Turn it on for code that reads `request.client.host` and
                cannot be changed.
                """
            ),
        ] = False,
    ) -> None:
        """Initialize the middleware with the trusted set."""
        self.app = app
        self.trusted = trusted
        self.overwrite_scope_client = overwrite_scope_client

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Resolve and cache the client address, then pass through."""
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        resolved = resolve_client_address(scope, self.trusted)
        if resolved is not None:
            state = scope.setdefault("state", {})
            state["client_address"] = resolved
            if self.overwrite_scope_client:
                scope["client"] = (resolved.key, resolved.port or 0)
        await self.app(scope, receive, send)
