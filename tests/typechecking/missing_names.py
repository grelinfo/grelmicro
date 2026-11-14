"""Type assertions for importing a name a lazy module does not serve.

Each marked line imports a name that does not exist, so ty and mypy report
it. The ignore comment is the assertion: both checkers fail on an ignore
comment that matches nothing, so a lazy loader that type checkers see fails
the check.
"""

from grelmicro import NoSuchName  # type: ignore[attr-defined]  # ty: ignore[import-not-found]
from grelmicro.integrations import fastapi
from grelmicro.integrations.fastapi import RateLimiterGone  # type: ignore[attr-defined]  # ty: ignore[import-not-found]
from grelmicro.resilience import NoSuchPolicy  # type: ignore[attr-defined]  # ty: ignore[import-not-found]


def missing_names() -> None:
    """Use each missing name."""
    print(NoSuchName, RateLimiterGone, NoSuchPolicy)  # noqa: T201
    print(fastapi.NoSuchName)  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]  # noqa: T201
