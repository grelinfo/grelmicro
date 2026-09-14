"""Fetching the documents a verifier loads its signing keys from.

An OIDC provider serves its signing keys over HTTP and rotates them. Fetching
them is network I/O, and verifying a token is not, so fetching lives here and
runs only when a verifier refreshes, never on the request path.

Read more in the [JWT](../security/jwt.md) docs.
"""

from __future__ import annotations

from importlib import import_module
from typing import Annotated, Any, Protocol

from typing_extensions import Doc

from grelmicro.errors import DependencyNotFoundError, GrelmicroError

__all__ = [
    "Fetcher",
    "SigningKeysUnavailableError",
    "fetch_with_httpx",
]


class SigningKeysUnavailableError(GrelmicroError, RuntimeError):
    """The signing keys could not be loaded.

    Raised by `refresh` when a document cannot be fetched or read, and by
    `verify` before any key set has loaded. A refresh that fails keeps the
    keys already loaded, so a provider that goes down does not take
    authentication down with it until those keys expire on its side.
    """


class Fetcher(Protocol):
    """Fetches a document a verifier loads its keys from.

    Supply your own to reuse a client that already carries your proxy
    settings, certificate authority, or mutual TLS identity, and to keep the
    request inside whatever tracing and retry policy that client has.
    """

    async def __call__(
        self,
        url: str,
        *,
        timeout: float,  # noqa: ASYNC109
        max_bytes: int,
    ) -> bytes:
        """Return the document body, refusing anything over `max_bytes`."""
        ...  # pragma: no cover


async def fetch_with_httpx(
    url: Annotated[str, Doc("The document to fetch.")],
    *,
    # The client applies this per connect, read and write phase, which one
    # cancel scope around the call cannot express, so it stays an argument.
    timeout: Annotated[float, Doc("Seconds to wait for the endpoint.")],  # noqa: ASYNC109
    max_bytes: Annotated[int, Doc("Largest body accepted.")],
) -> bytes:
    """Fetch a document with `httpx`, the default fetcher.

    Either `httpx` or `httpx2` will do, whichever the application already
    has, because the ecosystem is split across the two lines.

    The body is read in chunks and abandoned the moment it passes
    `max_bytes`, rather than trusting the length the server declares.
    Redirects are not followed: a key set that answers from somewhere else is
    a key set from somewhere else.
    """
    httpx = _httpx()

    async with (
        httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client,
        client.stream("GET", url) as response,
    ):
        if response.status_code != httpx.codes.OK:
            answered = f"endpoint answered {response.status_code}"
            raise SigningKeysUnavailableError(answered)
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > max_bytes:
                msg = f"document is larger than {max_bytes} bytes"
                raise SigningKeysUnavailableError(msg)
        return bytes(body)


def _httpx() -> Any:  # noqa: ANN401
    """Return whichever httpx the application has installed.

    Both lines are accepted because the ecosystem is split across them.
    FastAPI's `standard` extra pulls `httpx<1.0`, and Starlette's `full`
    extra pulls both, so either one can be the client an application already
    has. This fetcher touches only what the two have in common.

    `httpx` is tried first because it is what FastAPI installs today. Once
    FastAPI moves to `httpx2` alone, the other name can go and this becomes
    a plain import again.
    """
    for name in ("httpx", "httpx2"):
        try:
            return import_module(name)
        except ImportError:
            continue
    raise DependencyNotFoundError(module="httpx")
