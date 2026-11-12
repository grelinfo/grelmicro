"""Type assertions for the error classes the security modules served until 0.45.0.

Each class is gone from the module, so importing it from there is an error.
The ignore comment is the assertion: both checkers fail on an ignore comment
that matches nothing. The integration modules load their public names
lazily, so a type checker cannot tell a removed name from a lazy one there.
"""

from grelmicro.security.bans import AdmissionError  # type: ignore[attr-defined]  # ty: ignore[unresolved-import]
from grelmicro.security.jwt import GrelmicroError  # type: ignore[attr-defined]  # ty: ignore[unresolved-import]


def moved_names() -> None:
    """Use a removed name."""
    print(AdmissionError, GrelmicroError)  # noqa: T201
