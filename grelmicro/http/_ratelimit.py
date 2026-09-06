"""Rate limit decisions at the HTTP edge."""

from __future__ import annotations

from logging import getLogger
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro._paths import as_patterns, matches, route_path
from grelmicro.http._component import ErrorResponses, send_error
from grelmicro.resilience.errors import RateLimitExceededError
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

    from grelmicro.resilience._protocol import RateLimitResult
    from grelmicro.resilience.ratelimiter import RateLimiter
    from grelmicro.security.clientip import TrustedProxies

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = ["RateLimitMiddleware", "RateLimitedRequests"]

logger = getLogger("grelmicro.http.ratelimit")

_LEGACY_HEADERS = (
    b"x-ratelimit-limit",
    b"x-ratelimit-remaining",
    b"x-ratelimit-reset",
)
"""The superseded three fields, for a client that reads only those."""

_SF_UNSAFE = ('"', "\\")
"""What a structured field String cannot carry unescaped."""


def _policy_name(limiter: RateLimiter) -> str:
    """Return the limiter's name as a structured field String.

    Raises:
        ValueError: If the name cannot be written as one.
    """
    name = limiter.name
    if not name.isascii() or not name.isprintable():
        msg = (
            f"RateLimiter {name!r} cannot be named in a RateLimit header, "
            "which carries printable ASCII. Name the limiter in ASCII, or "
            "pass legacy_headers=True and no standard ones."
        )
        raise ValueError(msg)
    if any(character in name for character in _SF_UNSAFE):
        msg = (
            f"RateLimiter {name!r} cannot be named in a RateLimit header, "
            'which quotes the name, so it carries no `"` and no `\\`.'
        )
        raise ValueError(msg)
    return name


def _window_of(limiter: RateLimiter) -> int | None:
    """Return the seconds the limiter's quota is measured over.

    `None` for an algorithm that has no window. A token bucket refills
    continuously, so its `reset_after` is the wait for the next token
    rather than the edge of a window, and a `RateLimit-Policy` built from
    it would tell a client to expect a reset that never comes.
    """
    config = limiter._state.config  # noqa: SLF001
    if isinstance(config, SlidingWindowConfig):
        return int(config.window)
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
    served: list[tuple[bytes, bytes]] = []
    limits = ", ".join(
        f'"{_policy_name(limiter)}";r={max(result.remaining, 0)};'
        f"t={int(result.reset_after)}"
        for limiter, result in seen
    )
    served.append((b"ratelimit", limits.encode("latin-1")))
    policies = ", ".join(
        f'"{_policy_name(limiter)}";q={result.limit};w={window}'
        for limiter, result in seen
        if (window := _window_of(limiter)) is not None
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
                    str(int(result.reset_after)).encode("latin-1"),
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
                "The proxies whose forwarded entries may be believed, for "
                "resolving the caller. Not needed when "
                "`ClientAddressMiddleware` already resolved one, or when "
                "`key` builds the key itself."
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
                header cannot carry.
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
            ),
        )

    async def _spend(self, limiter: RateLimiter, key: str) -> RateLimitResult:
        """Take this request's tokens, waiting only if there is a budget."""
        if self._max_wait:
            return await limiter.wait(
                key=key, cost=self._cost, max_wait=self._max_wait
            )
        return await limiter.acquire(key=key, cost=self._cost)

    def _key_of(self, scope: Scope) -> str | None:
        """Return the bucket this request is metered under.

        The address `ClientAddressMiddleware` already resolved is reused
        where there is one, so an app running both walks the forwarded
        header once.
        """
        if self._key is not None:
            return self._key(scope)
        state = scope.setdefault("state", {})
        resolved = state.get("client_address")
        if resolved is None and self._trusted is not None:
            resolved = resolve_client_address(scope, self._trusted)
            if resolved is not None:
                # Kept where `ClientAddressMiddleware` keeps it, so a route
                # that meters itself reads the same caller rather than
                # walking the forwarded header a second time.
                state["client_address"] = resolved
        if resolved is None:
            self._report_no_caller()
            return None
        return resolved.key

    def _report_no_caller(self) -> None:
        """Say once that there is nobody to meter.

        A peer that cannot be read is the transport's, not the caller's,
        so it does not change from one request to the next and saying so
        on each of them would only fill the log.
        """
        if self._reported:
            logger.debug("rate limiter found no caller to meter")
            return
        self._reported = True
        logger.warning(
            "rate limiter found no caller to meter, letting the request "
            "through: the transport peer is absent or unparsable, and "
            "neither trusted= nor key= names another bucket"
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
            result = (
                await limiter.wait(key=key, cost=cost, max_wait=max_wait)
                if max_wait
                else await limiter.acquire(key=key, cost=cost)
            )
            seen.append((limiter, result))
            stated = {
                name.decode("latin-1"): value.decode("latin-1")
                for name, value in _rate_limit_headers(
                    seen, legacy=legacy_headers
                )
            }
            if not result.allowed:
                error = RateLimitExceededError(
                    key=key, retry_after=result.retry_after
                )
                error.headers = stated  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
                raise error
        return stated

    return spending()


def _stating(send: Send, headers: Sequence[tuple[bytes, bytes]]) -> Send:
    """Return a `send` that states what the caller has left."""

    async def stating(message: Message) -> None:
        if message["type"] == "http.response.start":
            message["headers"] = [*message["headers"], *headers]
        await send(message)

    return stating


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
