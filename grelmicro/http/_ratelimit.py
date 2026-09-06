"""Rate limit decisions at the HTTP edge."""

from __future__ import annotations

from logging import getLogger
from math import ceil, inf
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    NamedTuple,
    Self,
)

from typing_extensions import Doc

from grelmicro._paths import as_patterns, matches, route_path
from grelmicro.http._component import ErrorResponses, send_error
from grelmicro.resilience._protocol import RateLimitResult
from grelmicro.resilience.errors import RateLimitExceededError
from grelmicro.resilience.ratelimiter import _config_limit, _validate_cost
from grelmicro.resilience.ratelimiter.sliding_window import (
    SlidingWindowConfig,
)
from grelmicro.security.clientip import resolve_client_address

if TYPE_CHECKING:
    from collections.abc import (
        Awaitable,
        Callable,
        MutableMapping,
        Sequence,
    )
    from types import TracebackType

    from grelmicro.resilience.ratelimiter import RateLimiter
    from grelmicro.security.clientip import TrustedProxies

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = ["RateLimitMiddleware", "RateLimitedRequests"]

logger = getLogger("grelmicro.http.ratelimit")

_STATED = "grelmicro_rate_limit_stated"
"""Where a route leaves what it metered, for the middleware to state."""

_REMAINING_HEADER = b"x-ratelimit-remaining"
"""The superseded field the two meters are compared on."""

_LEGACY_HEADERS = (
    b"x-ratelimit-limit",
    _REMAINING_HEADER,
    b"x-ratelimit-reset",
)
"""The superseded three fields, for a client that reads only those."""

_SF_UNSAFE = ('"', "\\", ",", ";", "=")
"""What a policy name cannot carry: quoting, and the field's own punctuation."""


def _policy_name(limiter: RateLimiter) -> str:
    """Return the limiter's name as a structured field String.

    Raises:
        ValueError: If the name cannot be written as one.
    """
    name = limiter.name
    if not name.isascii() or not name.isprintable():
        msg = (
            f"RateLimiter {name!r} cannot be named in a RateLimit header, "
            "which carries printable ASCII. Name the limiter in printable "
            "ASCII: the name is what the header calls the policy."
        )
        raise ValueError(msg)
    if any(character in name for character in _SF_UNSAFE):
        msg = (
            f"RateLimiter {name!r} cannot be named in a RateLimit header. "
            "The name is quoted into it and read back out of it, so it "
            'carries no `"`, `\\`, `,`, `;` or `=`.'
        )
        raise ValueError(msg)
    return name


def check_route_limiters(
    limiters: Annotated[
        Sequence[RateLimiter], Doc("The limiters a route declares.")
    ],
    *,
    cost: Annotated[int, Doc("Tokens one call of it spends of each.")],
) -> None:
    """Refuse what a route could never meter, where it is written.

    The middleware checks the same two things when it is built. A route
    declares its own, so it checks them itself rather than failing every
    call it was supposed to meter.

    Raises:
        ValueError: If a limiter is named something a `RateLimit` header
            cannot carry, or `cost` is more than one of them can serve.
    """
    for limiter in limiters:
        _policy_name(limiter)
        _validate_cost(cost, _config_limit(limiter._state.config))  # noqa: SLF001


async def spend_one(
    limiter: Annotated[RateLimiter, Doc("The limiter to take tokens of.")],
    key: Annotated[str, Doc("The bucket the caller is metered under.")],
    *,
    cost: Annotated[int, Doc("Tokens the call spends.")],
    max_wait: Annotated[float, Doc("Seconds it waits before refusing.")],
) -> RateLimitResult:
    """Take one call's tokens, and return what the limiter decided.

    A budget that runs out is a refusal like any other, so the result
    says so rather than the wait raising it past the middleware that
    has to answer for it. What the limiter knew is kept: the quota from
    its own configuration, and the delay it named.

    A quota reconfigured below the cost this spends is a setting to
    fix, so the request is served and the reason said once.
    """
    try:
        if not max_wait:
            return await limiter.acquire(key=key, cost=cost)
        return await limiter.wait(key=key, cost=cost, max_wait=max_wait)
    except ValueError:
        # The quota was reconfigured under the cost this spends. It is
        # a setting to fix, and refusing every caller for it would take
        # the service down for a number that changed.
        logger.warning(
            "rate limiter %r cannot serve a cost of %d any more, "
            "letting requests through: its quota was reconfigured "
            "below it",
            limiter.name,
            cost,
            exc_info=True,
        )
        return RateLimitResult(
            allowed=True,
            limit=_config_limit(limiter._state.config),  # noqa: SLF001
            remaining=0,
            retry_after=0.0,
            reset_after=0.0,
        )
    except RateLimitExceededError as refusal:
        return RateLimitResult(
            allowed=False,
            limit=_config_limit(limiter._state.config),  # noqa: SLF001
            remaining=0,
            retry_after=refusal.retry_after,
            reset_after=refusal.retry_after,
        )


def _window_of(limiter: RateLimiter) -> int | None:
    """Return the seconds the limiter's quota is measured over.

    `None` for an algorithm that has no window, and for one shorter than
    the second the header is written in. A token bucket refills
    continuously, so its `reset_after` is the wait for the next token
    rather than the edge of a window, and a `RateLimit-Policy` built from
    it would tell a client to expect a reset that never comes. A window
    under a second would be published as a whole one, which is a rate a
    client pacing itself off it would be refused for. A longer one is
    rounded up for the same reason: published short, it invites a
    client to pace itself faster than the limiter allows.
    """
    config = limiter._state.config  # noqa: SLF001
    if isinstance(config, SlidingWindowConfig) and config.window >= 1:
        return ceil(config.window)
    return None


def _rate_limit_headers(
    seen: Sequence[tuple[RateLimiter, RateLimitResult]],
    *,
    legacy: bool,
) -> list[tuple[bytes, bytes]]:
    """Return the headers stating what every limiter had left.

    Both fields are Lists, so a service running a burst limit beside a
    daily one states both, which is what a client needs to know which one
    it is about to spend.
    """
    stated = [
        (_policy_name(limiter), result, _window_of(limiter))
        for limiter, result in seen
    ]
    served: list[tuple[bytes, bytes]] = [
        (
            b"ratelimit",
            ", ".join(
                f'"{name}";r={max(result.remaining, 0)};'
                f"t={ceil(result.reset_after)}"
                for name, result, _ in stated
            ).encode("latin-1"),
        )
    ]
    policies = ", ".join(
        f'"{name}";q={result.limit};w={window}'
        for name, result, window in stated
        if window is not None
    )
    if policies:
        served.append((b"ratelimit-policy", policies.encode("latin-1")))
    if legacy:
        _, result = min(seen, key=lambda item: item[1].remaining)
        served.extend(
            zip(
                _LEGACY_HEADERS,
                (
                    str(result.limit).encode("latin-1"),
                    str(max(result.remaining, 0)).encode("latin-1"),
                    str(ceil(result.reset_after)).encode("latin-1"),
                ),
                strict=True,
            )
        )
    return served


class RateLimitMiddleware:
    """Turn a caller away at the edge, and say what it has left.

    Every request spends one token of every limiter it is given, keyed by
    the caller. A request the first of them refuses is answered `429`
    without reaching the app, through the same `AdmissionError` path a
    handler's own refusal takes, so one exception handler still covers
    every way a caller is turned away.

    ```python
    from grelmicro.http import RateLimitMiddleware
    from grelmicro.resilience import RateLimiter
    from grelmicro.security import TrustedProxies

    app.add_middleware(
        RateLimitMiddleware,
        limiters=[RateLimiter.sliding_window("api", limit=100, window=60)],
        trusted=TrustedProxies(["10.0.0.0/8"]),
    )
    ```

    Register `RateLimitedRequests(...)` instead to have
    `micro.install(app)` add it for you.

    Allowed or refused, the response carries what the caller has left, in
    the two fields of the RateLimit header specification: `RateLimit` with
    `r=` remaining and `t=` seconds to reset, and `RateLimit-Policy` with
    `q=` quota and `w=` window for every limiter that has a window. A
    refusal adds `Retry-After`.

    The caller is the resolved client address, which is the socket peer
    unless a trusted proxy vouched for another one. A limiter keyed on the
    peer would meter the ingress rather than the caller, and one keyed on
    a raw `X-Forwarded-For` would meter whatever the caller wrote there.

    The middleware is pure ASGI and works with any ASGI framework
    (Starlette, Litestar, ...). It acts on `http` scopes and passes every
    other scope through untouched.
    """

    def __init__(
        self,
        app: Annotated[
            ASGIApp,
            Doc("The next ASGI application in the middleware chain."),
        ],
        *,
        limiters: Annotated[
            Sequence[RateLimiter],
            Doc(
                "The limiters every request spends. A request passes all "
                "of them or is refused by the first that says no."
            ),
        ],
        trusted: Annotated[
            TrustedProxies | None,
            Doc(
                "The proxies whose forwarded entries may be believed, "
                "for resolving the caller. Give this or `key`: without "
                "either, the only bucket left is the socket peer, which "
                "behind an ingress is the ingress. An address "
                "`ClientAddressMiddleware` already resolved is reused, "
                "and this says which proxies to believe when it has not."
            ),
        ] = None,
        key: Annotated[
            Callable[[Scope], str | None] | None,
            Doc(
                "Builds the bucket key from the ASGI scope, replacing the "
                "resolved caller. Return `None` to let a request through "
                "unmetered."
            ),
        ] = None,
        cost: Annotated[
            int,
            Doc("Tokens one request spends of every limiter."),
        ] = 1,
        max_wait: Annotated[
            float,
            Doc(
                "Seconds a throttled request waits for tokens before it is "
                "refused. `0.0` (the default) refuses as soon as the "
                "budget is spent, because waiting at the edge holds the "
                "connection open."
            ),
        ] = 0.0,
        exclude: Annotated[
            tuple[str, ...],
            Doc(
                "Paths this middleware leaves alone. Exact match unless "
                "the pattern ends with `*`, which matches as a prefix."
            ),
        ] = (),
        legacy_headers: Annotated[
            bool,
            Doc(
                "Also send `X-RateLimit-Limit`, `-Remaining` and `-Reset`, "
                "the superseded fields, for a client that reads only "
                "those. They carry the limiter closest to being spent."
            ),
        ] = False,
    ) -> None:
        """Initialize the middleware with the limiters it spends.

        Raises:
            TypeError: If no limiter is given, or the caller cannot be
                resolved because neither `trusted` nor `key` was given.
            ValueError: If a limiter is named something a `RateLimit`
                header cannot carry, or `cost` is more than one of them
                can ever serve.
        """
        self.app = app
        self._limiters = tuple(limiters)
        if not self._limiters:
            msg = (
                "RateLimitMiddleware takes at least one limiter. A "
                "middleware that meters nothing would let every request "
                "through while reporting that it is limited."
            )
            raise TypeError(msg)
        if trusted is None and key is None:
            msg = (
                "RateLimitMiddleware needs trusted= to resolve the caller "
                "it meters, or key= to build the bucket key itself. "
                "Without either, the only key left is the socket peer, "
                "which is the ingress rather than the caller behind it."
            )
            raise TypeError(msg)
        for limiter in self._limiters:
            _policy_name(limiter)
            _validate_cost(cost, _config_limit(limiter._state.config))  # noqa: SLF001
        self._trusted = trusted
        self._key = key
        self._cost = cost
        self._max_wait = max_wait
        self._exclude = as_patterns(exclude, name="exclude")
        self._legacy_headers = legacy_headers
        self._reported = False

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Spend the caller's tokens, then serve or refuse."""
        if scope["type"] != "http" or matches(route_path(scope), self._exclude):
            await self.app(scope, receive, send)
            return
        key = self._key_of(scope)
        if key is None:
            await self.app(scope, receive, send)
            return
        seen: list[tuple[RateLimiter, RateLimitResult]] = []
        for limiter in self._limiters:
            result = await self._spend(limiter, key)
            seen.append((limiter, result))
            if not result.allowed:
                await self._refuse(scope, send, key=key, seen=seen)
                return
        await self.app(
            scope,
            receive,
            _stating(
                send,
                _rate_limit_headers(seen, legacy=self._legacy_headers),
                scope,
            ),
        )

    async def _spend(self, limiter: RateLimiter, key: str) -> RateLimitResult:
        """Take this request's tokens, waiting only if there is a budget."""
        return await spend_one(
            limiter, key, cost=self._cost, max_wait=self._max_wait
        )

    def _key_of(self, scope: Scope) -> str | None:
        """Return the bucket this request is metered under."""
        bucket = bucket_of(scope, key=self._key, trusted=self._trusted)
        if bucket.key is None and self._key is None:
            self._report_no_caller(degraded=bucket.degraded)
        return bucket.key

    def _report_no_caller(self, *, degraded: bool) -> None:
        """Say once that there is nobody to meter, and which nobody.

        Neither cause changes from one request to the next, so saying
        it on each of them would only fill the log.
        """
        if self._reported:
            logger.debug("rate limiter found no caller to meter")
            return
        self._reported = True
        if degraded:
            logger.warning(
                "rate limiter found only one of your own proxies to "
                "meter, letting requests through: trusted= names a "
                "proxy that forwarded no caller, so every caller "
                "behind it would share one budget"
            )
            return
        logger.warning(
            "rate limiter found no caller to meter, letting the request "
            "through: no proxy vouched for one and the transport peer "
            "is absent or unreadable"
        )

    async def _refuse(
        self,
        scope: Scope,
        send: Send,
        *,
        key: str,
        seen: Sequence[tuple[RateLimiter, RateLimitResult]],
    ) -> None:
        """Answer `429` in the format the app answers every refusal with."""
        _, result = seen[-1]
        error = RateLimitExceededError(key=key, retry_after=result.retry_after)
        app = scope.get("app")
        registered = getattr(
            getattr(app, "state", None), "grelmicro_error_responses", None
        )
        errors = registered if registered is not None else ErrorResponses()
        rendered = errors.render(error, instance=scope["path"])
        if rendered is None:  # pragma: no cover - the kind is always known
            raise error
        for name, value in _rate_limit_headers(
            seen, legacy=self._legacy_headers
        ):
            rendered.headers[name.decode("latin-1")] = value.decode("latin-1")
        await send_error(send, rendered)


def spend(
    limiters: Annotated[
        Sequence[RateLimiter], Doc("The limiters this call spends.")
    ],
    key: Annotated[str, Doc("The bucket the caller is metered under.")],
    *,
    cost: Annotated[int, Doc("Tokens the call spends of each.")] = 1,
    max_wait: Annotated[float, Doc("Seconds it waits before refusing.")] = 0.0,
    legacy_headers: Annotated[
        bool, Doc("Also state the superseded fields.")
    ] = False,
) -> Any:  # noqa: ANN401
    """Return the coroutine spending one call's tokens.

    What the middleware does per request, for a route that meters itself.
    It returns the headers stating what is left, and raises
    `RateLimitExceededError` carrying them when a limiter says no, so the
    refusal reaches the client with the quota it just spent.
    """

    async def spending() -> dict[str, str]:
        seen: list[tuple[RateLimiter, RateLimitResult]] = []
        for limiter in limiters:
            result = await spend_one(limiter, key, cost=cost, max_wait=max_wait)
            seen.append((limiter, result))
            if not result.allowed:
                error = RateLimitExceededError(
                    key=key, retry_after=result.retry_after
                )
                error.headers = _stated(seen, legacy=legacy_headers)  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
                raise error
        return _stated(seen, legacy=legacy_headers)

    return spending()


def _stated(
    seen: Sequence[tuple[RateLimiter, RateLimitResult]], *, legacy: bool
) -> dict[str, str]:
    """Return the same headers, named the way a response sets them."""
    return {
        name.decode("latin-1"): value.decode("latin-1")
        for name, value in _rate_limit_headers(seen, legacy=legacy)
    }


def _stating(
    send: Send, headers: Sequence[tuple[bytes, bytes]], scope: Scope
) -> Send:
    """Return a `send` that states what the caller has left.

    A route metering itself has already stated its own quota, so the two
    are merged rather than appended: the standard fields are Lists and
    join into one, and the superseded ones are single integers that a
    client cannot read twice, so the route's stand.
    """
    ours = dict(headers)

    async def stating(message: Message) -> None:
        if message["type"] == "http.response.start":
            # Read here rather than at wrap time: the routes ran since.
            message["headers"] = _merged(
                message["headers"], _combined(_stated_on(scope), ours)
            )
        await send(message)

    return stating


def _has_less_left(
    already: Sequence[tuple[bytes, bytes]], ours: dict[bytes, bytes]
) -> bool:
    """Return whether what is already stated is the tighter budget.

    Only the superseded fields need this. They are single integers, so
    one of the two meters has to answer for both, and the one a caller
    will be refused by first is the honest one. A count that cannot be
    read is not one, so the other answers whatever it says.
    """
    theirs = _remaining(
        next(
            (
                value
                for name, value in already
                if name.lower() == _REMAINING_HEADER
            ),
            None,
        )
    )
    mine = _remaining(ours.get(_REMAINING_HEADER))
    if theirs is None and mine is None:
        return True
    if theirs is None:
        return False
    if mine is None:
        return True
    return theirs <= mine


def _remaining(value: bytes | None) -> int | None:
    """Return a stated remaining count, or `None` when it is not one.

    The value beside ours was written by something else, so a word
    where a number was expected leaves the two meters incomparable
    rather than failing a response the handler already produced.
    """
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def bucket_of(
    scope: Annotated[Scope, Doc("The ASGI scope of the request.")],
    *,
    key: Annotated[
        Callable[[Scope], str | None] | None,
        Doc("Builds the bucket itself, replacing the resolved caller."),
    ],
    trusted: Annotated[
        TrustedProxies | None,
        Doc("The proxies whose forwarded entries may be believed."),
    ],
) -> _Bucket:
    """Return the bucket this request is metered under, and why not.

    The address a middleware already resolved is reused, and one this
    resolves is left where the next reader looks, so the forwarded header
    is walked once however many meters a request passes.

    An address the walk could only take as far as one of your own
    proxies is nobody's bucket: metering every caller behind it as
    one would let any of them spend the budget of all of them. It is
    said so rather than logged here, because what to say about it
    belongs to whoever asked, and each of them says it once.
    """
    if key is not None:
        return _Bucket(key(scope), degraded=False)
    state = scope.setdefault("state", {})
    resolved = state.get("client_address")
    if resolved is None and trusted is not None:
        resolved = resolve_client_address(scope, trusted)
        if resolved is not None:
            state["client_address"] = resolved
    if resolved is None:
        return _Bucket(None, degraded=False)
    if resolved.degraded:
        return _Bucket(None, degraded=True)
    return _Bucket(resolved.key, degraded=False)


class _Bucket(NamedTuple):
    """The bucket a request is metered under, and why there is none."""

    key: str | None
    """What to meter under, or `None` when there is nobody to meter."""

    degraded: bool
    """Whether the only address left was one of your own proxies."""


def state_on(
    scope: Annotated[Scope, Doc("The ASGI scope of the request.")],
    headers: Annotated[
        dict[str, str], Doc("What one meter has to tell the caller.")
    ],
) -> dict[str, str]:
    """Leave what a route metered where the middleware will state it.

    A framework merges a dependency's headers into the response it
    builds, and not into a `Response` a handler returned itself. The
    middleware writes the response start either way, so what it is
    handed here reaches the caller whatever the route returned.

    A second meter on the same route joins the first rather than
    replacing it, so both budgets are stated and both are spent. What
    every meter on this request has stated so far is returned, for a
    caller that also has to write them somewhere itself.
    """
    stated = scope.setdefault("state", {}).setdefault(_STATED, {})
    combined = _combined(
        {
            name.lower().encode("latin-1"): value.encode("latin-1")
            for name, value in stated.items()
        },
        {
            name.lower().encode("latin-1"): value.encode("latin-1")
            for name, value in headers.items()
        },
    )
    stated.clear()
    stated.update(
        {
            name.decode("latin-1"): value.decode("latin-1")
            for name, value in combined.items()
        }
    )
    return dict(stated)


def _stated_on(scope: Scope) -> dict[bytes, bytes]:
    """Return what the routes under this request have already stated."""
    return {
        name.lower().encode("latin-1"): value.encode("latin-1")
        for name, value in scope.get("state", {}).get(_STATED, {}).items()
    }


def _combined(
    theirs: dict[bytes, bytes], ours: dict[bytes, bytes]
) -> dict[bytes, bytes]:
    """Return what two meters state as one set of fields.

    The standard fields are Lists and hold both, keyed by the policy
    name so the same meter counted twice appears once. The superseded
    ones are single integers, so the meter with less left answers.
    """
    combined = dict(theirs)
    for name, mine in ours.items():
        theirs_value = combined.get(name)
        if theirs_value is None or name in _LEGACY_HEADERS:
            combined[name] = mine
            continue
        combined[name] = _joined(theirs_value, mine)
    if _has_less_left(list(theirs.items()), ours):
        for name in _LEGACY_HEADERS:
            if name in theirs:
                combined[name] = theirs[name]
    return combined


def _joined(theirs: bytes, mine: bytes) -> bytes:
    """Return two Lists as one, with each policy named once.

    One policy metered twice is one policy, and the count a client
    has to pace itself off is the smaller of the two.
    """
    items: dict[bytes, bytes] = {}
    for value in (theirs, mine):
        for item in value.split(b", "):
            name = item.split(b";", 1)[0]
            seen = items.get(name)
            if seen is None or _left(item) <= _left(seen):
                items[name] = item
    return b", ".join(items.values())


def _left(item: bytes) -> float:
    """Return what one stated policy says is left, for comparing two."""
    for parameter in item.split(b";")[1:]:
        if parameter.startswith(b"r="):
            remaining = _remaining(parameter[2:])
            if remaining is not None:
                return remaining
    return inf


def _merged(
    already: Sequence[tuple[bytes, bytes]], ours: dict[bytes, bytes]
) -> list[tuple[bytes, bytes]]:
    """Return one field per name, with both meters in the Lists."""
    kept: list[tuple[bytes, bytes]] = []
    stated = dict(ours)
    narrower = _has_less_left(already, ours)
    for name, value in already:
        lowered = name.lower()
        mine = stated.pop(lowered, None)
        if mine is None:
            kept.append((name, value))
        elif lowered in _LEGACY_HEADERS:
            # A single integer a client cannot read twice, so the meter
            # with less left is the one that answers for the pair.
            kept.append((name, value if narrower else mine))
        else:
            kept.append((name, _joined(value, mine)))
    kept.extend(stated.items())
    return kept


class RateLimitedRequests:
    """Turn a caller away at the edge, wired by `micro.install(app)`.

    Register it and `install` adds `RateLimitMiddleware` to the app:

    ```python
    from grelmicro import Grelmicro
    from grelmicro.http import ErrorResponses, RateLimitedRequests
    from grelmicro.resilience import RateLimiter
    from grelmicro.security import TrustedProxies

    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                RateLimiter.sliding_window("burst", limit=100, window=60),
                RateLimiter.sliding_window("daily", limit=10000, window=86400),
                trusted=TrustedProxies(["10.0.0.0/8"]),
            ),
        ]
    )
    ```

    A request spends one token of every limiter listed, so a burst limit
    stands beside a daily one and the response states both.

    Every option of `RateLimitMiddleware` is taken here and forwarded, so
    a registered component and a hand-added middleware answer the same.

    A framework that serves no HTTP, such as FastStream, ignores it.

    Read more in the [Rate Limit](../http/rate-limit.md) docs.
    """

    kind: ClassVar[str] = "rate_limited_requests"

    def __init__(
        self,
        *limiters: Annotated[
            RateLimiter,
            Doc("The limiters every request spends, in the order given."),
        ],
        trusted: Annotated[
            TrustedProxies | None,
            Doc(
                "The proxies whose forwarded entries may be believed, for "
                "resolving the caller."
            ),
        ] = None,
        key: Annotated[
            Callable[[Scope], str | None] | None,
            Doc(
                "Builds the bucket key from the ASGI scope, replacing the "
                "resolved caller. Return `None` to leave a request "
                "unmetered."
            ),
        ] = None,
        cost: Annotated[
            int,
            Doc("Tokens one request spends of every limiter."),
        ] = 1,
        max_wait: Annotated[
            float,
            Doc(
                "Seconds a throttled request waits before it is refused. "
                "`0.0` refuses as soon as the budget is spent."
            ),
        ] = 0.0,
        exclude: Annotated[
            tuple[str, ...],
            Doc("Paths never metered. Same matching."),
        ] = (),
        legacy_headers: Annotated[
            bool,
            Doc("Also send the superseded `X-RateLimit-*` fields."),
        ] = False,
        name: Annotated[
            str,
            Doc("Registration name, for a second set of rules on one app."),
        ] = "default",
    ) -> None:
        """Meter every request through the registered middleware.

        Raises:
            TypeError: If no limiter is given, or neither `trusted` nor
                `key` says how to key the buckets.
            ValueError: If a limiter is named something a `RateLimit`
                header cannot carry.
        """
        self._name = name
        self._options: dict[str, Any] = {
            "limiters": limiters,
            "trusted": trusted,
            "key": key,
            "cost": cost,
            "max_wait": max_wait,
            "exclude": as_patterns(exclude, name="exclude"),
            "legacy_headers": legacy_headers,
        }
        # Built once here so a mistake is refused where it is written,
        # rather than on the first request the app serves.
        RateLimitMiddleware(_nothing, **self._options)

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    @property
    def limiters(self) -> tuple[RateLimiter, ...]:
        """Return the limiters every request spends."""
        return tuple(self._options["limiters"])

    def asgi_middleware(self) -> tuple[type[Any], dict[str, Any]]:
        """Return the middleware class and the arguments to build it with."""
        return RateLimitMiddleware, dict(self._options)

    def handled_exceptions(self) -> tuple[type[Exception], ...]:
        """Return what this component answers rather than letting through.

        The middleware answers what it refuses itself. This is what the
        limiter raises inside a handler, and registering the component is
        the opt-in for answering those the same way.
        """
        return (RateLimitExceededError,)

    async def __aenter__(self) -> Self:
        """Open the component.

        Nothing to open. The wiring happens in `micro.install(app)`, which
        reads the registration and adds the middleware to the framework
        before it serves. This is the declaration that it should.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Close the component. Nothing to close."""
        return None


async def _nothing(scope: Scope, receive: Receive, send: Send) -> None:
    """Stand in for the app, so the options can be checked without one."""
