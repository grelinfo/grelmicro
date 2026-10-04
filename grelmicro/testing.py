"""Test helpers for protocol-level interactions and authenticated apps.

`record(backend)` instruments a backend's public async methods in place and
returns a `CallLog`. The backend keeps its real type and behavior, so it drops
into a component exactly as before, while the log captures every call for
assertions, in the spirit of `pytest-mock`'s `mocker.spy`.

```python
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.testing import record

backend = MemoryLockAdapter()
log = record(backend)
micro = Grelmicro(uses=[Coordination(lock=backend)])

async with micro:
    await login("u1")

assert log.count("acquire", name="user:u1") == 1
```

`FakeVerifier` stands in for a `JWTVerifier`: each token is a name for the
claims it carries, built with `fake_claims`, so no key is generated and no
token is signed.

```python
from grelmicro.http import AuthenticatedRequests
from grelmicro.testing import FakeVerifier, fake_claims

verifier = FakeVerifier(alice=fake_claims("alice", "orders:read"))
micro = Grelmicro(uses=[AuthenticatedRequests(verifier)])
```
"""

from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

from grelmicro.security.jwt import (
    JWTClaims,
    TokenRejectedError,
    TokenRejectedReason,
    _frozen,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = [
    "Call",
    "CallLog",
    "FakeVerifier",
    "fake_claims",
    "record",
]

_BEARER = "bearer "

_REGISTERED = frozenset(
    {"aud", "exp", "iat", "iss", "jti", "nbf", "scope", "scopes", "scp", "sub"}
)
"""Claims `fake_claims` sets itself, refused among the extra claims."""


@dataclass(frozen=True)
class Call:
    """One recorded protocol call."""

    method: Annotated[str, Doc("Name of the method that was called.")]
    args: Annotated[
        tuple,
        Doc("Positional arguments the method was called with."),
    ] = field(default_factory=tuple)
    kwargs: Annotated[
        Mapping[str, Any],
        Doc("Keyword arguments the method was called with."),
    ] = field(default_factory=dict)


@dataclass
class CallLog:
    """Records calls made to an instrumented backend.

    Returned by `record(...)`. Exposes the raw `calls` list plus helpers to
    assert on what was called.
    """

    calls: Annotated[
        list[Call],
        Doc("Every recorded call, in order."),
    ] = field(default_factory=list)

    def count(
        self,
        method: Annotated[
            str | None,
            Doc("Method name to match, or `None` to count every call."),
        ] = None,
        args: Annotated[
            tuple | None,
            Doc("Positional arguments to match, or `None` to skip the check."),
        ] = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> int:
        """Return how many recorded calls match `method`, `args`, and `kwargs`.

        A call matches when its method equals `method` (when given), its
        positional arguments equal `args` (when given), and every item in
        `kwargs` equals the recorded keyword argument of the same name.
        """
        return sum(
            1
            for call in self.calls
            if (method is None or call.method == method)
            and (args is None or call.args == args)
            and all(
                key in call.kwargs and call.kwargs[key] == value
                for key, value in kwargs.items()
            )
        )

    def methods(self) -> list[str]:
        """Return the method names of every recorded call, in order."""
        return [call.method for call in self.calls]

    def reset(self) -> None:
        """Drop every recorded call."""
        self.calls.clear()


def record(
    backend: Annotated[
        object,
        Doc(
            """
            The backend instance to instrument. Its public async methods are
            wrapped in place, so the same instance keeps its type and behavior.
            """,
        ),
    ],
) -> CallLog:
    """Instrument `backend`'s public async methods and return their `CallLog`.

    Each public coroutine method (one whose name does not start with `_`) is
    replaced on the instance with a wrapper that records the call and forwards
    to the original. The class and other instances are untouched.
    """
    log = CallLog()
    for name in dir(backend):
        if name.startswith("_"):
            continue
        attr = getattr(backend, name)
        if inspect.iscoroutinefunction(attr):
            setattr(backend, name, _wrap(name, attr, log))
    return log


def _wrap(
    name: str,
    method: Callable[..., Any],
    log: CallLog,
) -> Callable[..., Any]:
    """Return an async wrapper that records the call then forwards to `method`."""

    @functools.wraps(method)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        log.calls.append(Call(name, args, dict(kwargs)))
        return await method(*args, **kwargs)

    return wrapper


def fake_claims(
    subject: Annotated[str, Doc("The `sub` claim, who the caller is.")],
    *scopes: Annotated[str, Doc("The scopes the caller holds.")],
    **claims: Annotated[object, Doc("Any other claim, such as `tenant`.")],
) -> JWTClaims:
    """Build the claims of a verified token, for a test.

    The registered claims other than `sub` stay unset, so the claims never
    expire. `claims` carries `sub`, `scope` when scopes are given, and the
    extra claims, read-only like a verified token's.

    Raises:
        TypeError: If a scope is not a string, an extra claim is a
            registered one such as `scope` or `exp`, or an extra claim is not
            a JSON value: a string, number, boolean, `None`, list or dict.
        ValueError: If `subject` is empty, or a scope is empty or holds
            whitespace, since a verified token never carries either.
    """
    if not subject:
        msg = "fake_claims() needs a non-empty subject."
        raise ValueError(msg)
    for scope in scopes:
        if not isinstance(scope, str):
            msg = f"fake_claims() takes each scope as a string, got {scope!r}."
            raise TypeError(msg)
        if not scope or scope != "".join(scope.split()):
            msg = (
                f"fake_claims() takes one scope per argument, got scope "
                f"{scope!r}. Pass each scope as its own argument."
            )
            raise ValueError(msg)
    clash = sorted(_REGISTERED.intersection(claims))
    if clash:
        names = ", ".join(clash)
        verb = (
            "is a registered claim"
            if len(clash) == 1
            else "are registered claims"
        )
        msg = (
            f"fake_claims(): {names} {verb}, not an extra claim. Pass the "
            "subject first and each scope as a positional argument."
        )
        raise TypeError(msg)
    for name, value in claims.items():
        if not _is_json(value):
            msg = (
                f"fake_claims(): claim {name!r} is {type(value).__name__}, "
                "not a JSON value a token can carry."
            )
            raise TypeError(msg)
    raw: dict[str, Any] = {"sub": subject}
    if scopes:
        raw["scope"] = " ".join(scopes)
    raw.update(claims)
    return JWTClaims(
        claims=_frozen(raw),
        subject=subject,
        issuer=None,
        audience=None,
        expires_at=None,
        issued_at=None,
        token_id=None,
        scopes=frozenset(scopes),
    )


def _is_json(value: object) -> bool:
    """Return whether `value` is a JSON value, as a token's claims are."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_is_json(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json(item)
            for key, item in value.items()
        )
    return False


class FakeVerifier:
    """A verifier for tests: each token is a name for the claims it carries.

    It answers `verify` and `verify_header` like `JWTVerifier`, so it drops
    into `AuthenticatedRequests`. A token it does not know is refused as
    `invalid`, and a header with no bearer token as `scheme`.

    ```python
    verifier = FakeVerifier(
        alice=fake_claims("alice", "orders:read"),
        bob=fake_claims("bob"),
    )
    verifier.verify_header("Bearer alice").subject  # "alice"
    ```
    """

    def __init__(
        self,
        tokens: Annotated[
            Mapping[str, JWTClaims] | None,
            Doc(
                "Tokens mapped to their claims, for a token that is not a name."
            ),
        ] = None,
        /,
        **named: Annotated[
            JWTClaims, Doc("Tokens given as keyword arguments.")
        ],
    ) -> None:
        """Map each token to the claims it stands for.

        Raises:
            TypeError: If a token maps to anything but `JWTClaims`, such as
                a mapping passed by keyword as `tokens=`.
        """
        self._tokens: dict[str, JWTClaims] = {**(tokens or {}), **named}
        for token, claims in self._tokens.items():
            if not isinstance(claims, JWTClaims):
                msg = (
                    f"FakeVerifier token {token!r} maps to "
                    f"{type(claims).__name__}, not JWTClaims. Build the claims "
                    "with fake_claims(), and pass a mapping of tokens as the "
                    "first positional argument."
                )
                raise TypeError(msg)

    def verify(
        self,
        token: Annotated[str, Doc("The token, with no scheme prefix.")],
    ) -> JWTClaims:
        """Return the claims of `token`, or raise `TokenRejectedError`."""
        try:
            return self._tokens[token]
        except KeyError:
            raise TokenRejectedError(TokenRejectedReason.INVALID) from None

    def verify_header(
        self,
        header: Annotated[
            str | None, Doc("The `Authorization` header value, or `None`.")
        ],
    ) -> JWTClaims:
        """Return the claims of the bearer token in `header`."""
        token = (header or "")[len(_BEARER) :].lstrip(" ")
        if not header or header[: len(_BEARER)].lower() != _BEARER or not token:
            raise TokenRejectedError(TokenRejectedReason.SCHEME)
        return self.verify(token)
