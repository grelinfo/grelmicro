"""Lock acquisition handle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from typing_extensions import Doc


@dataclass(frozen=True, slots=True)
class LockHandle:
    """The result of a successful lock acquisition.

    Returned by `Lock.acquire`, `Lock.acquire_nowait`, and `Lock.__aenter__`.
    Each acquisition produces its own handle, so a `Lock` shared by several
    tasks gives each holder a distinct handle.
    """

    name: Annotated[
        str,
        Doc("The lock name, as passed to `Lock(name)`."),
    ]
    token: Annotated[
        str,
        Doc(
            "The opaque ownership token the backend stored for this holder."
            " The same token is passed to `release` and `owned`."
        ),
    ]
    fencing_token: Annotated[
        int,
        Doc(
            "A strictly increasing integer minted by the backend for this"
            " lock name. It grows on every free-to-held transition (a new"
            " holder or a takeover of an expired lock) and keeps climbing"
            " across release and re-acquire cycles. A renewal by the same"
            " holder keeps the same value. Pass it to the protected resource"
            " so the resource can reject any write that carries a lower or"
            " equal token."
        ),
    ]
