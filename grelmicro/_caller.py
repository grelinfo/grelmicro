"""Who the caller of a request is, read the same way everywhere.

A route requirement, the security events, the access log, and idempotency
all read the caller an authentication layer left on the request scope. They
read it through here, so a caller one of them admits is never one another
names as nobody.
"""

from __future__ import annotations

__all__ = ["is_authenticated", "subject_of"]


def is_authenticated(caller: object) -> bool:
    """Return whether `caller` was authenticated.

    Only an `is_authenticated` that is `True` itself counts. A caller
    without one, or with a value that is merely truthy, was not
    authenticated.
    """
    return getattr(caller, "is_authenticated", False) is True


def subject_of(caller: object) -> str | None:
    """Return the subject of an authenticated caller, or `None`.

    A subject that is not a non-empty string names nobody. A caller whose
    attribute raises when read names nobody too, so recording what a
    request did never becomes an error of its own.
    """
    try:
        if not is_authenticated(caller):
            return None
        subject = getattr(caller, "subject", None)
    except Exception:  # noqa: BLE001
        return None
    return subject if isinstance(subject, str) and subject else None
