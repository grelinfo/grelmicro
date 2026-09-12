"""Short bans for clients that keep presenting tokens that do not verify.

A circuit breaker opens when the service it calls keeps failing. This opens
when the caller keeps failing, which is the same idea pointed the other way.

Verifying a forged token costs about as much as verifying a real one, because
the signature has to be checked before any claim can be trusted. A caller
sending forged tokens therefore costs real work per request. Counting those
failures and refusing the caller for a while turns that cost into a dictionary
lookup.

Rate limiting every request ahead of verification would also shed the load,
and costs more: it charges every honest request for traffic that is usually
not there. This charges nothing until a caller has already proven itself, and
then charges almost nothing.

Two things make this control dangerous if it is wired up carelessly, and both
are handled here rather than left to the caller:

Banning the wrong client. The identity counted must be one a caller cannot
choose, or an attacker sets a header and gets somebody else refused. Pass the
address `resolve_client_address` returns, never a raw `X-Forwarded-For`.

Being made to remember too much. Tracking every address that ever failed is
unbounded, and an attacker with an IPv6 allocation has more addresses than
anyone has memory. The table is bounded and evicts, so failing from many
addresses buys an attacker nothing that failing from one does not.

Read more in the [JWT](../security/jwt.md) docs.
"""

from __future__ import annotations

from collections import deque
from time import monotonic
from typing import Annotated, Any, Final

from pydantic import BaseModel, field_validator
from typing_extensions import Doc

from grelmicro.errors import GrelmicroError

__all__ = [
    "ABUSIVE_REASONS",
    "ClientBannedError",
    "ClientBans",
    "ClientBansConfig",
]

ABUSIVE_REASONS: Final = frozenset({"algorithm", "malformed", "signature"})
"""Rejection reasons that mean the caller is trying something, by default.

These three say the token was never issued by anyone the service trusts: a
signature that does not check out, an algorithm the verifier does not accept,
or bytes that are not a token at all. Nothing a working client does produces
them.

The reasons left out matter more than the ones kept. `unknown-key` is what
every client sees for a moment when the provider rotates its signing keys, and
counting it would ban a service's real users on every rotation. `expired` is a
client whose token needs refreshing, which is ordinary. `not-yet-valid` is a
clock that disagrees. `audience` and `issuer` are a token meant for a
neighbouring service, which is a misrouted client rather than an attacker.
"""


class ClientBannedError(GrelmicroError, RuntimeError):
    """The caller is banned, so its token was not looked at.

    Distinct from `TokenRejectedError` because it says nothing about the
    token. Answer it with `429`, not `401`: the caller is being refused for
    what it did before this request, and a fresh token would not change it.
    """

    def __init__(self) -> None:
        """Initialize the error."""
        super().__init__("Too many rejected tokens from this client.")


class ClientBansConfig(BaseModel):
    """When a client is refused, and for how long."""

    failures: Annotated[
        int,
        Doc("Failures inside `window` before the client is banned."),
    ] = 10
    window: Annotated[
        float,
        Doc("Seconds over which failures are counted."),
    ] = 60.0
    duration: Annotated[
        float,
        Doc(
            "Seconds a ban lasts. Keep it short. Addresses are shared behind"
            " NAT, so a ban reaches more people than the one caller that"
            " earned it."
        ),
    ] = 300.0
    max_clients: Annotated[
        int,
        Doc(
            "Addresses tracked at once. Reached, the least recently recorded"
            " are dropped, so failing from many addresses costs an attacker"
            " the bans they had rather than the service its memory."
        ),
    ] = 10_000

    @field_validator("failures", "max_clients")
    @classmethod
    def _check_counted(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse a count that would ban everyone or remember no one."""
        if value < 1:
            msg = "value must be at least one"
            raise ValueError(msg)
        return value

    @field_validator("window", "duration")
    @classmethod
    def _check_duration(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse a duration that is zero or below."""
        if value <= 0:
            msg = "value must be greater than zero"
            raise ValueError(msg)
        return value


class ClientBans:
    """Tracks failing clients and refuses the ones that keep failing.

    `banned` is the only call on the request path and does one dictionary
    lookup. `record` runs when a token was already refused, so it is never on
    the path of a request that succeeds.

    Example:
        ```python
        bans = ClientBans()

        if bans.banned(client_ip):
            raise HTTPException(status_code=429)

        try:
            claims = verifier.verify_header(authorization)
        except TokenRejectedError as error:
            bans.record(client_ip, error.reason)
            raise
        ```
    """

    def __init__(
        self,
        config: Annotated[
            ClientBansConfig | None, Doc("When to ban, and for how long.")
        ] = None,
        *,
        reasons: Annotated[
            frozenset[str] | None,
            Doc("Rejection reasons that count. Defaults to `ABUSIVE_REASONS`."),
        ] = None,
    ) -> None:
        """Initialize the table, which starts empty."""
        settings = config or ClientBansConfig()
        self._failures = settings.failures
        self._window = settings.window
        self._duration = settings.duration
        self._max_clients = settings.max_clients
        self._reasons = ABUSIVE_REASONS if reasons is None else reasons
        # `(window_started, count, banned_until)` per client. Kept beside a
        # queue of the order clients were first seen, so making room never
        # walks the table. Every operation on either is one the interpreter
        # applies whole, so this is safe to share across a thread pool and
        # under a free-threaded interpreter, with no lock on the read path.
        self._clients: dict[str, tuple[float, int, float]] = {}
        self._order: deque[str] = deque()

    def banned(
        self,
        client: Annotated[
            str,
            Doc("The address a trusted proxy vouched for, never a raw header."),
        ],
    ) -> bool:
        """Whether `client` is currently refused.

        One dictionary lookup, and nothing is written, so an honest request
        pays this and no more.
        """
        seen = self._clients.get(client)
        return seen is not None and seen[2] > monotonic()

    def record(
        self,
        client: Annotated[str, Doc("The address the failure came from.")],
        reason: Annotated[str, Doc("The `TokenRejectedError` reason.")],
    ) -> bool:
        """Count a rejection, and return whether it banned the client.

        A reason outside the configured set is not counted at all, so a key
        rotation or a batch of expired tokens never bans anyone.
        """
        if reason not in self._reasons:
            return False
        now = monotonic()
        seen = self._clients.get(client)
        if seen is None or now - seen[0] >= self._window:
            started, count = now, 1
        else:
            started, count = seen[0], seen[1] + 1
        banned_until = now + self._duration if count >= self._failures else 0.0
        self._make_room()
        if client not in self._clients:
            self._order.append(client)
        self._clients[client] = (started, count, banned_until)
        return banned_until > 0.0

    def forget(
        self, client: Annotated[str, Doc("The address to clear.")]
    ) -> None:
        """Drop everything remembered about `client`, ban included."""
        self._clients.pop(client, None)

    def _make_room(self) -> None:
        """Drop the oldest entries so the table stays bounded.

        Taken from the end the queue was written at, so nothing reads the
        table while another thread is writing to it.
        """
        clients = self._clients
        order = self._order
        while len(clients) >= self._max_clients:
            try:
                oldest = order.popleft()
            except IndexError:
                break
            clients.pop(oldest, None)
