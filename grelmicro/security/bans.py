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

import asyncio
import threading
import weakref
from collections import OrderedDict
from time import monotonic
from typing import TYPE_CHECKING, Annotated, Any, Final, Self

from pydantic import BaseModel, field_validator
from typing_extensions import Doc

from grelmicro._config import Reconfigurable, env_prefixes, resolve_config
from grelmicro.errors import AdmissionError
from grelmicro.metrics import _hub
from grelmicro.security._events import BANS_ACTIVE, ban_started
from grelmicro.security.jwt import TokenRejectedReason

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "ABUSIVE_REASONS",
    "ClientBannedError",
    "ClientBans",
    "ClientBansConfig",
]

ABUSIVE_REASONS: Final[frozenset[TokenRejectedReason]] = frozenset(
    {TokenRejectedReason.ALGORITHM, TokenRejectedReason.SIGNATURE}
)
"""Rejection reasons that mean the caller is trying something, by default.

These two say a token was built to pass as one the service trusts: a
signature that does not check out, or an algorithm its key does not verify.
Both cost a full verification to refuse, and nothing a working client does
produces them.

The reasons left out matter more than the ones kept. `unknown-key` is what
every client sees for a moment when the provider rotates its signing keys,
and counting it would ban a service's real users on every rotation.
`malformed` is refused for almost nothing, before any signature is checked,
and a legitimate client presenting an opaque token lands there. `expired` is
a client whose token needs refreshing, which is ordinary. `not-yet-valid` is
a clock that disagrees. `audience` and `issuer` are a token meant for a
neighbouring service, which is a misrouted client rather than an attacker.
"""


class ClientBannedError(AdmissionError, RuntimeError):
    """The caller is banned, so its token was not looked at.

    Distinct from `TokenRejectedError` because it says nothing about the
    token. Answer it with `429`, not `401`: the caller is being refused for
    what it did before this request, and a fresh token would not change it.
    `retry_after` says how long the ban has left.
    """

    def __init__(
        self,
        *,
        retry_after: Annotated[
            float, Doc("Seconds the ban has left, `0.0` when not known.")
        ] = 0.0,
    ) -> None:
        """Initialize the error."""
        self.retry_after = retry_after
        super().__init__("Too many rejected tokens from this client.")


class ClientBansConfig(BaseModel, frozen=True):
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


class ClientBans(Reconfigurable[ClientBansConfig]):
    """Tracks failing clients and refuses the ones that keep failing.

    `banned` is the only call on the request path and does one dictionary
    lookup. `record` runs when a token was already refused, so it is never on
    the path of a request that succeeds.

    Built with keywords, it also reads `GREL_CLIENTBANS_`, or
    `GREL_CLIENTBANS_{NAME}_` for a named table, once `GREL_ENV_LOAD` is set.
    Every setting can change while the service runs: a ban only ever costs
    capacity, never trust.

    Example:
        ```python
        bans = ClientBans()

        if bans.banned(client_ip):
            raise ClientBannedError(retry_after=bans.banned_for(client_ip))

        try:
            claims = verifier.verify_header(authorization)
        except TokenRejectedError as error:
            bans.record(client_ip, error.reason)
            raise
        ```
    """

    def __init__(
        self,
        *,
        failures: Annotated[
            int | None,
            Doc("Failures inside `window` before the client is banned."),
        ] = None,
        window: Annotated[
            float | None,
            Doc("Seconds over which failures are counted."),
        ] = None,
        duration: Annotated[
            float | None,
            Doc("Seconds a ban lasts. Keep it short: addresses are shared."),
        ] = None,
        max_clients: Annotated[
            int | None,
            Doc("Addresses tracked at once."),
        ] = None,
        reasons: Annotated[
            frozenset[str] | None,
            Doc("Rejection reasons that count. Defaults to `ABUSIVE_REASONS`."),
        ] = None,
        name: Annotated[
            str,
            Doc(
                "Instance name, which is the environment namespace:"
                " `GREL_CLIENTBANS_{NAME}_`. The default instance reads"
                " `GREL_CLIENTBANS_`."
            ),
        ] = "default",
        env_load: Annotated[
            bool | None,
            Doc(
                "Whether to read environment variables. `None` follows the"
                " process-wide `GREL_ENV_LOAD` flag."
            ),
        ] = None,
    ) -> None:
        """Initialize the table, which starts empty.

        A setting left out is read from the environment, then takes the
        default `ClientBansConfig` gives it.

        Raises:
            SettingsValidationError: If a setting is refused.
        """
        env_prefix, kind_prefix = env_prefixes("CLIENTBANS", name)
        config = resolve_config(
            ClientBansConfig,
            explicit=None,
            kwargs={
                "failures": failures,
                "window": window,
                "duration": duration,
                "max_clients": max_clients,
            },
            env_prefix=env_prefix,
            kind_env_prefix=kind_prefix,
            env_load=env_load,
        )
        self._setup(config, reasons=reasons, name=name)
        self._track_reconfigure(env_prefix)

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            ClientBansConfig, Doc("When to ban, and for how long.")
        ],
        *,
        reasons: Annotated[
            frozenset[str] | None,
            Doc("Rejection reasons that count. Defaults to `ABUSIVE_REASONS`."),
        ] = None,
        name: Annotated[
            str,
            Doc("Instance name, carried by the ban records and metrics."),
        ] = "default",
    ) -> Self:
        """Build the table from a configuration that is already whole.

        The one declarative door. What you pass is what runs: no environment
        variable is read, and the table is not registered for live reload.
        """
        instance = cls.__new__(cls)
        instance._setup(config, reasons=reasons, name=name)  # noqa: SLF001
        return instance

    def _setup(
        self,
        config: ClientBansConfig,
        *,
        reasons: frozenset[str] | None,
        name: str,
    ) -> None:
        """Hold the settings, and start an empty table."""
        self._name = name
        self._config = config
        self._reconfigure_lock = asyncio.Lock()
        self._take(config)
        self._reasons = ABUSIVE_REASONS if reasons is None else reasons
        # `(window_started, count, banned_until)` per client, the least
        # recently recorded first. Only a rejected token writes, and it writes
        # under the lock, so a request that succeeds never takes it: `banned`
        # stays one lookup, safe beside a writer on a thread pool and under a
        # free-threaded interpreter.
        self._clients: OrderedDict[str, tuple[float, int, float]] = (
            OrderedDict()
        )
        self._lock = threading.Lock()
        _TABLES.add(self)

    @property
    def name(self) -> str:
        """Return the instance name."""
        return self._name

    def active(self) -> int:
        """Return how many clients are banned right now."""
        with self._lock:
            now = monotonic()
            return sum(1 for seen in self._clients.values() if seen[2] > now)

    def _take(self, config: ClientBansConfig) -> None:
        """Read the thresholds a request is judged against."""
        self._failures = config.failures
        self._window = config.window
        self._duration = config.duration
        self._max_clients = config.max_clients

    async def _apply_reconfigure(self, new_config: ClientBansConfig) -> None:
        """Take the new thresholds. Clients already tracked keep their counts."""
        self._take(new_config)

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

    def banned_for(
        self,
        client: Annotated[
            str,
            Doc("The address a trusted proxy vouched for, never a raw header."),
        ],
    ) -> float:
        """Return the seconds `client` stays refused, `0.0` when it is not.

        Read once `banned` has said yes, to tell the caller when to come back,
        so an honest request never pays for it.
        """
        seen = self._clients.get(client)
        if seen is None:
            return 0.0
        return max(seen[2] - monotonic(), 0.0)

    def record(
        self,
        client: Annotated[str, Doc("The address the failure came from.")],
        reason: Annotated[str, Doc("The `TokenRejectedError` reason.")],
    ) -> bool:
        """Count a rejection, and return whether it banned the client.

        A reason outside the configured set is not counted at all, so a key
        rotation or a batch of expired tokens never bans anyone.

        A ban already running is never shortened. The counting window is
        shorter than a ban, so a client that keeps failing rolls its window
        over while still banned, and taking the new count at face value would
        let it clear its own ban by carrying on. `forget` is what lifts a ban.

        A ban that starts writes one record on `grelmicro.security.events`
        and counts `grelmicro.client_bans.started`. A ban extended while it
        runs writes nothing.
        """
        if reason not in self._reasons:
            return False
        with self._lock:
            now = monotonic()
            clients = self._clients
            seen = clients.get(client)
            if seen is None or now - seen[0] >= self._window:
                started, count = now, 1
            else:
                started, count = seen[0], seen[1] + 1
            banned_until = (
                now + self._duration if count >= self._failures else 0.0
            )
            running_until = 0.0 if seen is None else seen[2]
            running = running_until > now
            if running:
                banned_until = max(banned_until, running_until)
            starts = banned_until > 0.0 and not running
            duration = self._duration
            if seen is None:
                # Only an address not yet tracked makes room. Making it for
                # one already tracked would let a client failing from a single
                # address evict every other entry, bans included.
                self._make_room()
            clients[client] = (started, count, banned_until)
            # The client just recorded goes last, so the one evicted to make
            # room is always the one that failed longest ago.
            clients.move_to_end(client)
        if starts:
            ban_started(self._name, client, count, duration)
        return banned_until > 0.0

    def forget(
        self, client: Annotated[str, Doc("The address to clear.")]
    ) -> None:
        """Drop everything remembered about `client`, ban included."""
        with self._lock:
            self._clients.pop(client, None)

    def _make_room(self) -> None:
        """Evict the least recently recorded clients so a new one fits.

        Called with the lock held, so no other writer changes the table
        underneath it.
        """
        clients = self._clients
        while len(clients) >= self._max_clients:
            clients.popitem(last=False)


_TABLES: weakref.WeakSet[ClientBans] = weakref.WeakSet()
"""Every ban table alive, read by the active bans gauge."""


def _observe_active(options: Any) -> Iterator[Any]:  # noqa: ANN401, ARG001
    """Report the bans running in each table, when metrics are collected."""
    from opentelemetry.metrics import Observation  # noqa: PLC0415

    for table in list(_TABLES):
        yield Observation(
            table.active(), {"grelmicro.client_bans.name": table.name}
        )


_hub.observe_with(BANS_ACTIVE, _observe_active, "{ban}")
