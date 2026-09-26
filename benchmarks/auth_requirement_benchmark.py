"""Benchmark the scope check an authenticated route runs on each request.

Run with: python benchmarks/auth_requirement_benchmark.py

Measures `Requirement.caller`, which Starlette and Litestar build once per
route, and `requirement_for(...).caller`, which FastAPI looks up on each
request because its scopes arrive with it. The reference line rebuilds both
scope sets on every request, which is what the check cost before it
was built once.
"""

from __future__ import annotations

import sys
import timeit
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from grelmicro.http._requirement import Requirement, requirement_for
from grelmicro.security.jwt import JWTClaims

ROUNDS = 1_000_000
REPEAT = 7

SCOPES = ("orders:read",)
SCOPE: dict[str, Any] = {
    "type": "http",
    "user": JWTClaims(
        claims={},
        subject="user-1",
        issuer="https://issuer.example",
        audience="orders",
        expires_at=None,
        issued_at=None,
        token_id=None,
        scopes=frozenset({"orders:read", "orders:write"}),
    ),
}


def _rebuilt_per_request(scope: dict[str, Any]) -> Any:  # noqa: ANN401
    """Check the caller the way it was checked before, for reference."""
    caller = scope.get("user")
    if caller is None or not getattr(caller, "is_authenticated", False):
        raise AssertionError
    if not set(SCOPES) <= set(getattr(caller, "scopes", ())):
        raise AssertionError
    return caller


def _measure(label: str, check: Any) -> None:  # noqa: ANN401
    """Print nanoseconds per call, the best of `REPEAT` runs."""
    best = min(timeit.repeat(check, number=ROUNDS, repeat=REPEAT))
    print(f"{label:<44} {best / ROUNDS * 1e9:6.0f} ns/request")  # noqa: T201


def main() -> None:
    """Measure each way a route checks its caller."""
    requirement = Requirement(SCOPES)
    fastapi_scopes = list(SCOPES)
    _measure(
        "sets rebuilt per request (reference)",
        lambda: _rebuilt_per_request(SCOPE),
    )
    _measure(
        "built once per route (Starlette, Litestar)",
        lambda: requirement.caller(SCOPE),
    )
    _measure(
        "looked up per request (FastAPI)",
        lambda: requirement_for(fastapi_scopes).caller(SCOPE),
    )


if __name__ == "__main__":
    main()
