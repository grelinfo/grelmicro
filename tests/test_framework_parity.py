"""Every pattern behaves the same on every framework, discovered, not listed.

The [Frameworks](../docs/frameworks.md) page claims that every pattern works
on every framework grelmicro supports, and that the only difference is the
wiring `micro.install(app)` does. A claim like that is worth making only
while a test holds it, so this is the test.

Two tiers:

1. **Every framework, HTTP or not.** The same pattern resolved ambiently
   inside a request handler and inside a message subscriber answers the
   same. FastStream included: the subscriber path and the request path are
   wired differently, which is what makes it worth proving.
2. **Every HTTP framework.** What reaches the wire is identical, byte for
   byte. FastStream is excluded because it serves no HTTP, not by name but
   because it carries no HTTP-facing hook to call.

The patterns are found by walking the package for ambient resolution rather
than by listing them, so one added tomorrow is covered without anyone
remembering to add it here, and the sweep refuses to pass on an empty scan
the way `tests/test_backend_contracts.py` does.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from litestar import Litestar, post, put
from litestar.testing import TestClient as LitestarTestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient as StarletteTestClient

import grelmicro
from grelmicro import Grelmicro
from grelmicro.cache import JsonSerializer, TTLCache, cached
from grelmicro.coordination import Lock, ReadWriteLock, TaskLock
from grelmicro.health import HealthChecks
from grelmicro.http import (
    PROBLEM_MEDIA_TYPE,
    ConditionalRequests,
    ErrorResponses,
    IdempotentRequests,
    check_precondition,
)
from grelmicro.http._tmf import TMF_MEDIA_TYPE
from grelmicro.idempotency import Idempotency
from grelmicro.integrations import faststream as faststream_integration
from grelmicro.outbox import Outbox
from grelmicro.outbox.memory import MemoryOutboxAdapter
from grelmicro.providers.memory import MemoryProvider
from grelmicro.resilience import Bulkhead, CircuitBreaker, RateLimiter
from tests._contract_support import called_names, module_name

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from starlette.requests import Request

    Exercise = Callable[[], Coroutine[Any, Any, dict[str, Any]]]
    Answer = tuple[int, dict[str, str], bytes]
    Answers = dict[str, Answer]

faststream = pytest.importorskip("faststream")
faststream_redis = pytest.importorskip("faststream.redis")

from faststream import FastStream  # noqa: E402
from faststream.redis import RedisBroker, TestRedisBroker  # noqa: E402

pytestmark = [pytest.mark.timeout(30)]

PACKAGE_ROOT = Path(grelmicro.__file__).parent


# --- The sweep -----------------------------------------------------------


def _resolves_ambiently(node: ast.AST) -> bool:
    """Return whether this definition looks the active app up.

    Both doors count: `resolve_ambient(...)`, which every pattern takes to
    reach its component, and `Grelmicro.current()`, which the rest use.
    """
    names = called_names(node)
    return "resolve_ambient" in names or (
        "Grelmicro" in names and "current" in names
    )


def _discover_ambient() -> list[str]:
    """Return every public class or function that resolves through the app."""
    found: set[str] = set()
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = module_name(path)
        if module.startswith("tests"):  # pragma: no cover
            continue
        for node in tree.body:
            if not isinstance(
                node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            if node.name.startswith("_"):
                continue
            if _resolves_ambiently(node):
                found.add(node.name)
    return sorted(found)


AMBIENT = _discover_ambient()

_MIN_AMBIENT = 13
"""Floor for the sweep, so an empty scan cannot pass silently."""


NOT_PER_REQUEST = {
    "LeaderElection": (
        "Elects across replicas over its own lease, so it belongs to the app "
        "lifecycle rather than to one request."
    ),
    "CronTask": (
        "Claims a fire against the schedule backend on the task loop, which "
        "no request handler enters."
    ),
}
"""Ambient resolvers a request handler never runs, and why.

Both resolve the same `Coordination` component the locks do, and both do it
from the task the app opened rather than from a handler. The lifecycle they
run under is `async with micro:`, which `install` opens the same way on
every framework.
"""

FASTAPI_ONLY = {
    "health_router": "Builds a FastAPI `APIRouter`. #690 tracks a framework-free endpoint.",
    "metrics_router": "Builds a FastAPI `APIRouter`, same as `health_router`.",
}
"""The honest exceptions: what one framework has and the others do not."""


# --- The exercises -------------------------------------------------------


async def _lock() -> dict[str, Any]:
    async with Lock("parity") as handle:
        return {"fencing_token": handle.fencing_token}


async def _readwritelock() -> dict[str, Any]:
    lock = ReadWriteLock("parity")
    async with lock.read as reading:
        generation = reading.generation
    async with lock.write as writing:
        return {
            "generation": generation,
            "fencing_token": writing.fencing_token,
        }


async def _tasklock() -> dict[str, Any]:
    async with TaskLock("parity"):
        return {"held": True}


async def _ttlcache() -> dict[str, Any]:
    cache = TTLCache(ttl=60, serializer=JsonSerializer())
    await cache.set("parity", {"n": 1})
    return {"value": await cache.get("parity")}


async def _cached() -> dict[str, Any]:
    calls: list[int] = []

    @cached(ttl=60)
    async def double(number: int) -> int:
        calls.append(number)
        return number * 2

    first = await double(21)
    second = await double(21)
    return {"first": first, "second": second, "calls": len(calls)}


async def _ratelimiter() -> dict[str, Any]:
    limiter = RateLimiter.sliding_window("parity", limit=5, window=60)
    result = await limiter.acquire(key="client")
    return {
        "allowed": result.allowed,
        "limit": result.limit,
        "remaining": result.remaining,
    }


async def _circuitbreaker() -> dict[str, Any]:
    breaker = CircuitBreaker("parity")
    async with breaker:
        pass
    return {"state": str(breaker.metrics().state)}


async def _bulkhead() -> dict[str, Any]:
    async with Bulkhead("parity", max_concurrent=2):
        return {"entered": True}


async def _outbox() -> dict[str, Any]:
    staged = await Outbox.current().publish(None, "parity.topic", {"n": 1})
    return {"staged": staged}


async def _healthchecks() -> dict[str, Any]:
    report = await Grelmicro.current().get("health").run()
    return {"status": str(report["status"])}


async def _idempotency() -> dict[str, Any]:
    idempotency = Idempotency("parity")
    async with idempotency("key") as operation:
        if not operation.replayed:
            operation.store({"n": 1})
    async with idempotency("key") as replay:
        return {"replayed": replay.replayed, "result": replay.result()}


EXERCISES: dict[str, Exercise] = {
    "Bulkhead": _bulkhead,
    "CircuitBreaker": _circuitbreaker,
    "HealthChecks": _healthchecks,
    "Idempotency": _idempotency,
    "Lock": _lock,
    "Outbox": _outbox,
    "RateLimiter": _ratelimiter,
    "ReadWriteLock": _readwritelock,
    "TTLCache": _ttlcache,
    "TaskLock": _tasklock,
    "cached": _cached,
}
"""One call per pattern, written the way a handler would write it.

Each resolves its backend ambiently, with no `backend=` anywhere, so what is
under test is the binding the framework's `install` put in place. Each
returns what it observed, so a framework that resolved something else fails
on the value rather than on an exception.
"""


def test_the_sweep_finds_the_ambient_patterns() -> None:
    """An empty or shrunken scan is a failure, never a silent pass."""
    assert len(AMBIENT) >= _MIN_AMBIENT, (
        f"expected at least {_MIN_AMBIENT} ambient resolvers, found {AMBIENT}"
    )


def test_every_ambient_pattern_is_covered_or_named() -> None:
    """A pattern added tomorrow is exercised, or says why it is not.

    The three ways out are all explicit: an exercise below, a reason it
    never runs per request, or a reason one framework alone has it.
    """
    classified = set(EXERCISES) | set(NOT_PER_REQUEST) | set(FASTAPI_ONLY)
    missing = sorted(set(AMBIENT) - classified)
    assert not missing, (
        f"these resolve through the active app but no framework matrix "
        f"covers them: {missing}. Add an exercise to EXERCISES, or a reason "
        f"to NOT_PER_REQUEST or FASTAPI_ONLY."
    )


# --- Tier 1: every framework answers the same ----------------------------


def _micro() -> Grelmicro:
    """Build an app carrying every component the exercises resolve.

    Memory backends throughout, and a fresh app per run, so two frameworks
    that behave the same produce the same values rather than merely the
    same shape.
    """
    return Grelmicro(
        uses=[
            MemoryProvider(),
            HealthChecks(),
            Outbox(MemoryOutboxAdapter(), relay=False, requires="process"),
        ]
    )


def _on_fastapi(exercise: Exercise) -> dict[str, Any]:
    """Answer from a FastAPI route handler."""
    micro = _micro()
    app = FastAPI()

    @app.post("/parity")
    async def parity() -> dict[str, Any]:
        return await exercise()

    micro.install(app)
    with TestClient(app) as client:
        return client.post("/parity").json()


def _on_starlette(exercise: Exercise) -> dict[str, Any]:
    """Answer from a Starlette route handler."""
    micro = _micro()

    async def parity(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(await exercise())

    app = Starlette(routes=[Route("/parity", parity, methods=["POST"])])
    micro.install(app)
    with StarletteTestClient(app) as client:
        return client.post("/parity").json()


def _on_litestar(exercise: Exercise) -> dict[str, Any]:
    """Answer from a Litestar route handler."""
    micro = _micro()

    @post("/parity")
    async def parity() -> dict[str, Any]:
        return await exercise()

    app = Litestar(route_handlers=[parity])
    micro.install(app)
    with LitestarTestClient(app=app) as client:
        return client.post("/parity").json()


def _on_faststream(exercise: Exercise) -> dict[str, Any]:
    """Answer from a FastStream message subscriber."""

    async def answer() -> dict[str, Any]:
        micro = _micro()
        broker = RedisBroker()
        app = FastStream(broker)

        @broker.subscriber("parity")
        async def parity(message: str) -> dict[str, Any]:  # noqa: ARG001
            return await exercise()

        micro.install(app)
        async with TestRedisBroker(broker):
            await app.start()
            try:
                response = await broker.request("run", "parity")
            finally:
                await app.stop()
        return json.loads(response.body)

    return anyio.run(answer)


FRAMEWORKS = {
    "fastapi": _on_fastapi,
    "starlette": _on_starlette,
    "litestar": _on_litestar,
    "faststream": _on_faststream,
}
"""Every framework `micro.install(app)` knows, HTTP or not."""


@pytest.mark.parametrize("pattern", sorted(EXERCISES))
def test_every_framework_resolves_the_same_pattern(pattern: str) -> None:
    """The same call inside a handler answers the same on every framework.

    Not merely "it did not raise". The value the pattern reported has to
    match, so a framework whose binding resolved a different component, or
    none, fails here.
    """
    # Act
    answers = {
        name: run(EXERCISES[pattern]) for name, run in FRAMEWORKS.items()
    }

    # Assert
    assert len(answers) == len(FRAMEWORKS)
    reference = answers["fastapi"]
    for name, answer in answers.items():
        assert answer == reference, name


# --- Tier 2: every HTTP framework answers the same bytes ------------------


_KEY = "5f9d2c1e"
"""One idempotency key, reused by every framework so the stored key matches."""


def _charge_body(amount: int) -> dict[str, int]:
    """Return the body every framework's handler answers with."""
    return {"amount": amount}


def _idempotent_fastapi() -> Answers:
    """Serve one charge twice through the FastAPI integration."""
    micro = _idempotent_micro()
    app = FastAPI()

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        return _charge_body(100)

    @app.put("/carts/1")
    async def replace() -> dict[str, int]:
        return _stale_write()

    micro.install(app)
    with TestClient(app) as client:
        return _replay_answers(client)


def _idempotent_starlette() -> Answers:
    """Serve the same charge through the Starlette integration."""
    micro = _idempotent_micro()

    async def charge(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(_charge_body(100))

    async def replace(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(_stale_write())

    app = Starlette(
        routes=[
            Route("/charge", charge, methods=["POST"]),
            Route("/carts/1", replace, methods=["PUT"]),
        ]
    )
    micro.install(app)
    with StarletteTestClient(app) as client:
        return _replay_answers(client)


def _idempotent_litestar() -> Answers:
    """Serve the same charge through the Litestar integration."""
    micro = _idempotent_micro()

    # Litestar answers a POST with `201` by default and the other two with
    # `200`. That is the framework's choice of handler status, not anything
    # the middleware does, so it is set here rather than compared.
    @post("/charge", status_code=200)
    async def charge() -> dict[str, int]:
        return _charge_body(100)

    @put("/carts/1", status_code=200)
    async def replace() -> dict[str, int]:
        return _stale_write()

    app = Litestar(route_handlers=[charge, replace])
    micro.install(app)
    with LitestarTestClient(app=app) as client:
        return _replay_answers(client)


HTTP_FRAMEWORKS = {
    "fastapi": _idempotent_fastapi,
    "starlette": _idempotent_starlette,
    "litestar": _idempotent_litestar,
}
"""Every framework the middleware reaches the wire on.

FastStream is absent because it serves no HTTP. It carries no
`install_middleware`, so `micro.install(app)` skips it.
"""


def _idempotent_micro() -> Grelmicro:
    """Register the middleware the way a service would, through `uses=[...]`."""
    return Grelmicro(
        uses=[
            MemoryProvider(),
            ErrorResponses(),
            IdempotentRequests(
                ttl=60,
                require_key=True,
                fingerprint_body=True,
                max_body_size=64,
            ),
            ConditionalRequests(),
        ]
    )


_VERSION = 3
"""The version every framework's resource carries, so one tag matches all."""

HTTP_412_PRECONDITION_FAILED = 412
"""What a stale `If-Match` is answered with."""


def _stale_write() -> dict[str, int]:
    """Refuse a write whose `If-Match` is not the current version."""
    check_precondition(_VERSION)
    return {"version": _VERSION}


def _answer(response: Any) -> Answer:  # noqa: ANN401
    """Reduce a response to what a client can observe."""
    return response.status_code, dict(response.headers), response.content


def _replay_answers(client: Any) -> Answers:  # noqa: ANN401
    """Run the three cases every framework has to answer the same way."""
    headers = {"Idempotency-Key": _KEY}
    return {
        "fresh": _answer(
            client.post("/charge", headers=headers, json={"n": 1})
        ),
        "replay": _answer(
            client.post("/charge", headers=headers, json={"n": 1})
        ),
        # Every way a client can get this wrong, and what each is answered
        # with. The status is the standard's, and the bytes are the same
        # whichever framework served them.
        "no_key": _answer(client.post("/charge", json={"n": 1})),
        "long_key": _answer(
            client.post(
                "/charge",
                headers={"Idempotency-Key": "x" * 256},
                json={"n": 1},
            )
        ),
        "reused_key": _answer(
            client.post("/charge", headers=headers, json={"n": 2})
        ),
        "body_too_large": _answer(
            client.post(
                "/charge",
                headers={"Idempotency-Key": "big"},
                json={"n": "x" * 128},
            )
        ),
        "stale": _answer(client.put("/carts/1", headers={"If-Match": '"2"'})),
        "unconditional": _answer(client.put("/carts/1")),
    }


EXPECTED_STATUS = {
    "fresh": 200,
    "replay": 200,
    "no_key": 400,
    "long_key": 400,
    "reused_key": 422,
    "body_too_large": 413,
    "stale": 412,
    "unconditional": 428,
}
"""What each case is answered with, by the standard rather than by us.

`400` for a key that is missing or malformed, `413` for a body larger than
the service reads, `422` for a key reused with a different payload, `412`
for a precondition that no longer holds, and `428` for a write that had to
be conditional and was not.
"""


def test_every_case_answers_the_status_the_standard_gives_it() -> None:
    """A client error is a `4xx`, on every framework, never a `500`."""
    # Act
    answers = {name: serve() for name, serve in HTTP_FRAMEWORKS.items()}

    # Assert
    for name, answer in answers.items():
        for case, expected in EXPECTED_STATUS.items():
            assert answer[case][0] == expected, f"{name}: {case}"


def test_every_http_framework_replays_identically() -> None:
    """One registration, one wire behaviour, whichever framework serves it.

    The bar is the one `test_every_http_framework_answers_identically` set
    for a rejection: the status line, every header, and the body byte for
    byte, so a client cannot tell which framework answered and a service can
    move between them without its callers noticing.
    """
    # Act
    answers = {name: serve() for name, serve in HTTP_FRAMEWORKS.items()}

    # Assert
    assert len(answers) == len(HTTP_FRAMEWORKS)
    reference = answers["fastapi"]
    for name, answer in answers.items():
        for case, observed in answer.items():
            assert observed == reference[case], f"{name}: {case}"


def test_the_replay_repeats_the_first_response() -> None:
    """A retry gets the first answer back, marked as a replay."""
    # Act
    answers = _idempotent_fastapi()

    # Assert
    fresh_status, fresh_headers, fresh_body = answers["fresh"]
    replay_status, replay_headers, replay_body = answers["replay"]
    assert replay_status == fresh_status
    assert replay_body == fresh_body
    assert replay_headers.pop("idempotent-replayed") == "true"
    assert replay_headers == fresh_headers


def test_a_framework_without_http_takes_no_middleware() -> None:
    """A framework that serves no HTTP is skipped, not special-cased by name."""
    # Assert
    assert not hasattr(faststream_integration, "install_middleware")


def _tmf_micro() -> Grelmicro:
    """Register the TM Forum format, which the middleware must answer in."""
    return Grelmicro(
        uses=[
            MemoryProvider(),
            ErrorResponses.tmf(),
            IdempotentRequests(ttl=60, require_key=True),
            ConditionalRequests(require_precondition=("PUT",)),
        ]
    )


def _tmf_fastapi() -> Answers:
    """Refuse through the FastAPI integration, in the registered format."""
    app = FastAPI()

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        return _charge_body(100)

    @app.put("/carts/1")
    async def replace() -> dict[str, int]:
        return _stale_write()

    _tmf_micro().install(app)
    with TestClient(app) as client:
        return _refusal_answers(client)


def _tmf_starlette() -> Answers:
    """Refuse the same two through the Starlette integration."""

    async def charge(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(_charge_body(100))

    async def replace(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(_stale_write())

    app = Starlette(
        routes=[
            Route("/charge", charge, methods=["POST"]),
            Route("/carts/1", replace, methods=["PUT"]),
        ]
    )
    _tmf_micro().install(app)
    with StarletteTestClient(app) as client:
        return _refusal_answers(client)


def _tmf_litestar() -> Answers:
    """Refuse the same two through the Litestar integration."""

    @post("/charge", status_code=200)
    async def charge() -> dict[str, int]:
        return _charge_body(100)

    @put("/carts/1", status_code=200)
    async def replace() -> dict[str, int]:
        return _stale_write()

    app = Litestar(route_handlers=[charge, replace])
    _tmf_micro().install(app)
    with LitestarTestClient(app=app) as client:
        return _refusal_answers(client)


def _refusal_answers(client: Any) -> Answers:  # noqa: ANN401
    """Run the two refusals a middleware writes without reaching a handler."""
    return {
        # `IdempotencyMiddleware` refuses a missing key.
        "no_key": _answer(client.post("/charge")),
        # `ConditionalRequestsMiddleware` refuses an unconditional write.
        "no_precondition": _answer(client.put("/carts/1")),
    }


TMF_FRAMEWORKS = {
    "fastapi": _tmf_fastapi,
    "starlette": _tmf_starlette,
    "litestar": _tmf_litestar,
}
"""Every framework the middleware refusals reach the wire on."""


def test_a_middleware_refusal_follows_the_registered_format() -> None:
    """A middleware answers in the format the app registered, not its own.

    A middleware runs outside the routing layer, so no exception handler
    sees what it decides and nothing else can reshape it. It has to read
    the registered `ErrorResponses` itself, on every framework, or a
    service that publishes TMF answers RFC 9457 from its edge and two
    shapes reach one client.
    """
    # Act
    answers = {name: serve() for name, serve in TMF_FRAMEWORKS.items()}

    # Assert
    assert len(answers) == len(TMF_FRAMEWORKS)
    reference = answers["fastapi"]
    for name, answer in answers.items():
        for case, (status, headers, body) in answer.items():
            assert headers["content-type"] == TMF_MEDIA_TYPE, f"{name}: {case}"
            assert b'"code"' in body, f"{name}: {case}"
            assert b'"reason"' in body, f"{name}: {case}"
            assert (status, headers, body) == reference[case], f"{name}: {case}"


def _bare_micro() -> Grelmicro:
    """Register the component alone, with nothing to choose a format."""
    return Grelmicro(uses=[ConditionalRequests()])


def _bare_fastapi() -> Answer:
    """Refuse a stale write on FastAPI, with no `ErrorResponses`."""
    app = FastAPI()

    @app.put("/carts/1")
    async def replace() -> dict[str, int]:
        return _stale_write()

    _bare_micro().install(app)
    with TestClient(app) as client:
        return _answer(client.put("/carts/1", headers={"If-Match": '"2"'}))


def _bare_starlette() -> Answer:
    """Refuse the same stale write on Starlette."""

    async def replace(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(_stale_write())

    app = Starlette(routes=[Route("/carts/1", replace, methods=["PUT"])])
    _bare_micro().install(app)
    with StarletteTestClient(app) as client:
        return _answer(client.put("/carts/1", headers={"If-Match": '"2"'}))


def _bare_litestar() -> Answer:
    """Refuse the same stale write on Litestar."""

    @put("/carts/1", status_code=200)
    async def replace() -> dict[str, int]:
        return _stale_write()

    app = Litestar(route_handlers=[replace])
    _bare_micro().install(app)
    with LitestarTestClient(app=app) as client:
        return _answer(client.put("/carts/1", headers={"If-Match": '"2"'}))


BARE_FRAMEWORKS = {
    "fastapi": _bare_fastapi,
    "starlette": _bare_starlette,
    "litestar": _bare_litestar,
}
"""Every HTTP framework, carrying the component and nothing else."""


def test_a_component_answers_without_error_responses() -> None:
    """Registering the component is the whole opt-in, on every framework.

    `ErrorResponses` chooses the format for the app. A service that never
    registered one still gets the status its component decided, in RFC 9457,
    rather than a `500` that says nothing.
    """
    # Act
    answers = {name: refuse() for name, refuse in BARE_FRAMEWORKS.items()}

    # Assert
    assert len(answers) == len(BARE_FRAMEWORKS)
    reference = answers["fastapi"]
    for name, answer in answers.items():
        status, headers, body = answer
        assert status == HTTP_412_PRECONDITION_FAILED, name
        assert headers["content-type"] == PROBLEM_MEDIA_TYPE, name
        assert (status, headers, body) == reference, name
