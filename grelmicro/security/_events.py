"""Security events: what authentication refused, and whom it banned.

One structured record per refusal on `grelmicro.security.events`, a counter
of every authentication attempt, and the refusal on the current span. The
record carries the Elastic Common Schema categorization, so a SIEM rule
reading `event.category` and `event.outcome` finds it, and an OpenTelemetry
event name, so the logging bridge sends it as a named event tied to the
request's span.

Nothing here reads a token. A subject is only ever one the verifier read
from a token whose signature verified.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from datetime import UTC, datetime
from time import monotonic, time
from typing import TYPE_CHECKING, Any, Final

from grelmicro._asgi import client_address
from grelmicro.metrics import _emit
from grelmicro.trace._otel import get as _otel

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    Scope = MutableMapping[str, Any]

__all__ = [
    "ATTEMPTS",
    "AUTHENTICATION_REFUSED",
    "AUTHORIZATION_REFUSED",
    "BANS_ACTIVE",
    "BAN_STARTED",
    "SCOPE_KEY",
    "SecurityEvents",
    "ban_started",
    "encoded",
    "logger",
]

logger = logging.getLogger("grelmicro.security.events")
"""Where every security event is written. Route it to your SIEM."""

AUTHENTICATION_REFUSED: Final = "grelmicro.authentication.refused"
"""The event name of a request refused before a caller was established."""

AUTHORIZATION_REFUSED: Final = "grelmicro.authorization.refused"
"""The event name of an authenticated caller refused for a missing scope."""

BAN_STARTED: Final = "grelmicro.client_bans.started"
"""The event name of a ban starting, and the counter of bans started."""

ATTEMPTS: Final = "grelmicro.authentication.attempts"
"""Counter of requests authentication verified or refused, one per request."""

AUTHORIZATION_REFUSALS: Final = "grelmicro.authorization.refusals"
"""Counter of authenticated callers refused for a missing scope."""

BANS_ACTIVE: Final = "grelmicro.client_bans.active"
"""Gauge of the bans running, read when metrics are collected."""

REFUSAL: Final = "grelmicro.authentication.refusal"
"""The span attribute naming why the request was refused."""

ENDUSER: Final = "enduser.id"
"""The attribute naming the authenticated end user."""

SUPPRESSED: Final = "grelmicro.security.suppressed"
"""How many refusals of one kind from one address were not written since the last."""

SCOPE_KEY: Final = "grelmicro.security_events"
"""Where the middleware leaves its recorder for a refusal a route raises."""

AUTHENTICATION_REQUIRED: Final = "authentication-required"
"""The refusal of a request that carried no credential."""

CLIENT_BANNED: Final = "client-banned"
"""The refusal of a request from a banned address."""

REPEAT_INTERVAL: Final = 60.0
"""Seconds one address writes at most one record of each refusal in."""

TRACKED_CLIENTS: Final = 10_000
"""Address and refusal pairs whose repeats are counted at once."""

VALUE_LIMIT: Final = 256
"""Characters kept of a value taken from the request."""

_SUCCESS: Final = {"grelmicro.outcome": "success"}
"""The attributes of an attempt that authenticated."""

_UNSAFE: Final = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
"""Characters that could end or forge a line in a log sink."""


def encoded(value: str) -> str:
    """Return `value` cut to `VALUE_LIMIT` characters, line breaks escaped.

    A control character is written as its escape, so a value taken from a
    request can never start a record of its own in a sink that writes one
    record per line.
    """
    if len(value) > VALUE_LIMIT:
        value = f"{value[:VALUE_LIMIT]}..."
    return _UNSAFE.sub(_escape, value)


def _escape(match: re.Match[str]) -> str:
    """Return the escape a control character is written as."""
    code = ord(match.group())
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"  # noqa: PLR2004


def _set_on_span(key: str, value: str) -> None:
    """Set `key` on the current span, when one is recording."""
    otel = _otel()
    if otel is None:
        return
    span = otel.trace.get_current_span()
    if span.is_recording():
        span.set_attribute(key, value)


def subject_of(caller: object) -> str | None:
    """Return the subject of an authenticated caller, or `None`.

    A caller whose attribute raises when read names nobody, so recording a
    refusal never becomes an error of its own.
    """
    try:
        if getattr(caller, "is_authenticated", False) is not True:
            return None
        subject = getattr(caller, "subject", None)
    except Exception:  # noqa: BLE001
        return None
    return subject if isinstance(subject, str) and subject else None


class _Repeats:
    """Counts the refusals of each kind from each address since its last record.

    The first refusal of a kind from an address is written. The same kind
    from the same address inside `REPEAT_INTERVAL` is counted, and the next
    one written after the interval carries the count. A refusal of another
    kind has a count of its own, so a request with no token never holds back
    a forged one. The table is bounded, and the pair recorded longest ago is
    dropped first.
    """

    __slots__ = ("_clients", "_interval", "_limit", "_lock")

    def __init__(self, interval: float, limit: int) -> None:
        """Start with no address."""
        self._interval = interval
        self._limit = limit
        self._clients: OrderedDict[tuple[str, str], tuple[float, int]] = (
            OrderedDict()
        )
        self._lock = threading.Lock()

    def admit(self, client: str, refusal: str) -> int | None:
        """Return how many refusals were held back, or `None` to hold this one."""
        key = (client, refusal)
        with self._lock:
            now = monotonic()
            clients = self._clients
            seen = clients.get(key)
            if seen is not None and now - seen[0] < self._interval:
                clients[key] = (seen[0], seen[1] + 1)
                clients.move_to_end(key)
                return None
            if seen is None:
                while len(clients) >= self._limit:
                    clients.popitem(last=False)
            clients[key] = (now, 0)
            clients.move_to_end(key)
            return 0 if seen is None else seen[1]


class SecurityEvents:
    """Records what one authentication middleware refused and accepted."""

    __slots__ = ("_enduser", "_repeats")

    def __init__(self, *, enduser: bool) -> None:
        """Record with or without the subject of the caller."""
        self._enduser = enduser
        self._repeats = _Repeats(REPEAT_INTERVAL, TRACKED_CLIENTS)

    def authenticated(self, caller: object) -> None:
        """Count an attempt that authenticated, and name the caller on the span."""
        _emit.incr(ATTEMPTS, _SUCCESS, unit="{attempt}")
        if self._enduser:
            subject = subject_of(caller)
            if subject is not None:
                _set_on_span(ENDUSER, subject)

    def refused(
        self,
        scope: Scope,
        *,
        refusal: str,
        status: int,
        template: str | None,
        subject: str | None,
    ) -> None:
        """Count a refusal, mark the span, and write the record.

        A request refused while its address is banned is counted and marked,
        never written: the ban's own record already says so. A refusal of a
        kind already written for the address inside the interval is counted
        into the next record instead.

        A missing scope counts on `AUTHORIZATION_REFUSALS`, because the
        request already counted as one that authenticated.
        """
        attributes: dict[str, Any] = {"error.type": refusal}
        if template is not None:
            attributes["http.route"] = template
        if status == 403:  # noqa: PLR2004
            _emit.incr(AUTHORIZATION_REFUSALS, attributes, unit="{refusal}")
        else:
            attributes["grelmicro.outcome"] = "refused"
            _emit.incr(ATTEMPTS, attributes, unit="{attempt}")
        _set_on_span(REFUSAL, refusal)
        named = subject if self._enduser else None
        if named is not None:
            _set_on_span(ENDUSER, named)
        if refusal == CLIENT_BANNED:
            return
        level = (
            logging.INFO
            if refusal == AUTHENTICATION_REQUIRED
            else logging.WARNING
        )
        if not logger.isEnabledFor(level):
            return
        client = client_address(scope)
        suppressed = self._repeats.admit(client or "", refusal)
        if suppressed is None:
            return
        self._write(
            scope,
            level=level,
            refusal=refusal,
            status=status,
            template=template,
            client=client,
            subject=named,
            suppressed=suppressed,
        )

    def _write(
        self,
        scope: Scope,
        *,
        level: int,
        refusal: str,
        status: int,
        template: str | None,
        client: str | None,
        subject: str | None,
        suppressed: int,
    ) -> None:
        """Write the record of one refusal."""
        authorization = status == 403  # noqa: PLR2004
        name = (
            AUTHORIZATION_REFUSED if authorization else AUTHENTICATION_REFUSED
        )
        fields: dict[str, Any] = {
            "otel.event.name": name,
            "event.kind": "event",
            "event.category": ["web"] if authorization else ["authentication"],
            "event.type": ["access"] if authorization else ["start"],
            "event.outcome": "failure",
            "event.action": name,
            "error.type": refusal,
            "http.response.status_code": status,
        }
        websocket = scope.get("type") == "websocket"
        if websocket:
            method = "WEBSOCKET"
            fields["network.protocol.name"] = "websocket"
        else:
            method = encoded(str(scope.get("method", "")))
            fields["http.request.method"] = method
        if template is not None:
            fields["http.route"] = encoded(template)
        if client is not None:
            fields["client.address"] = encoded(client)
        agent = _header(scope, b"user-agent")
        if agent is not None:
            fields["user_agent.original"] = encoded(agent)
        if subject is not None:
            fields[ENDUSER] = encoded(subject)
        if suppressed:
            fields[SUPPRESSED] = suppressed
        logger.log(
            level,
            "%s %s %s %s",
            method,
            fields.get("http.route", "-"),
            status,
            refusal,
            extra=fields,
        )


def ban_started(name: str, client: str, failures: int, duration: float) -> None:
    """Count a ban that just started, and write its record."""
    _emit.incr(BAN_STARTED, {"grelmicro.client_bans.name": name}, unit="{ban}")
    if not logger.isEnabledFor(logging.WARNING):
        return
    address = encoded(client)
    logger.warning(
        "client %s banned for %ss after %s failures",
        address,
        duration,
        failures,
        extra={
            "otel.event.name": BAN_STARTED,
            "event.kind": "event",
            "event.category": ["intrusion_detection"],
            "event.type": ["denied"],
            "event.outcome": "success",
            "event.action": BAN_STARTED,
            "client.address": address,
            "grelmicro.client_bans.name": name,
            "grelmicro.client_bans.failures": failures,
            "grelmicro.client_bans.duration": duration,
            "grelmicro.client_bans.until": datetime.fromtimestamp(
                time() + duration, UTC
            ).isoformat(),
        },
    )


def _header(scope: Scope, name: bytes) -> str | None:
    """Return one request header, or `None` when it was not sent."""
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1", "replace")
    return None
