"""Tests for `Grelmicro.describe()` and the `grelmicro check` command."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING, Any, Self

import pytest
from fastapi import APIRouter, Depends, FastAPI, Security
from fastapi.security import APIKeyHeader
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.authentication import AuthenticationBackend
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route, Router

from grelmicro import Grelmicro
from grelmicro.__main__ import TargetError, load_target, main
from grelmicro._describe import (
    AppReport,
    ComponentReport,
    _mask,
    describe_provider,
)
from grelmicro._discovery import load_integration
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.describe import AppReport as PublicAppReport
from grelmicro.health import HealthChecks
from grelmicro.http import (
    AuthenticatedRequests,
    CachedResponses,
    CachedResponsesConfig,
    IdempotentRequests,
)
from grelmicro.integrations.fastapi import Authenticated, CachedResponse
from grelmicro.providers._base import Provider
from grelmicro.providers.memory import MemoryProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.sqlite import SQLiteProvider
from tests.test_authentication import verifier

if TYPE_CHECKING:
    from pathlib import Path

    from grelmicro.cache._protocol import CacheBackend

_USAGE_ERROR = 2
"""Exit code for a target that cannot be resolved, distinct from a failed check."""

_TIMEOUT = 5.0
"""A non-credential config value, which must survive masking untouched."""


def test_describe_lists_components() -> None:
    """Every registered component appears with its kind, name, and backend."""
    micro = Grelmicro(
        uses=[Cache(MemoryCacheAdapter()), HealthChecks()],
        environment="development",
    )

    report = micro.describe()

    assert isinstance(report, AppReport)
    assert report.environment == "development"
    kinds = {component.kind for component in report.components}
    assert kinds == {"cache", "health"}
    cache = next(c for c in report.components if c.kind == "cache")
    assert cache.name == "default"
    assert cache.backends == ("MemoryCacheAdapter",)


def test_describe_reports_declined_provider_kinds() -> None:
    """A Provider says which kinds it does not serve, not only which it does.

    This is the answer to "why is my outbox unwired", which today is
    swallowed as a `NotImplementedError` inside the default wiring.
    """
    micro = Grelmicro(
        uses=[SQLiteProvider("sqlite:///:memory:")],
        environment="development",
    )

    report = micro.describe()

    sqlite = next(p for p in report.providers if p.short_name == "sqlite")
    assert "outbox" in sqlite.declines
    assert "leaderelection" in sqlite.serves
    assert "cache" in sqlite.serves


def test_describe_masks_provider_credentials() -> None:
    """A password in a Provider URL never reaches the report."""
    micro = Grelmicro(
        uses=[RedisProvider("redis://user:hunter2@localhost:6379/0")],
        environment="development",
    )

    report = micro.describe()

    redis = next(p for p in report.providers if p.short_name == "redis")
    assert redis.url is not None
    assert "hunter2" not in redis.url
    assert "***" in redis.url
    assert "hunter2" not in report.render()


def test_describe_reports_provider_env_prefix() -> None:
    """A Provider names the variables that repoint it."""
    micro = Grelmicro(
        uses=[RedisProvider("redis://localhost:6379/0")],
        environment="development",
    )

    report = micro.describe()

    redis = next(p for p in report.providers if p.short_name == "redis")
    assert redis.env_prefix == "REDIS_"
    assert "REDIS_*" in report.render()


def test_describe_scope_check_passes_in_development() -> None:
    """A memory backend is the point of `development`, so it is not a failure."""
    micro = Grelmicro(uses=[MemoryProvider()], environment="development")

    report = micro.describe()

    assert report.ok
    assert all(check.status != "fail" for check in report.checks)


def test_describe_scope_check_fails_in_production() -> None:
    """The same memory backend fails the check in a strict tier."""
    micro = Grelmicro(uses=[MemoryProvider()], environment="production")

    report = micro.describe()

    assert not report.ok
    assert any(
        check.name == "backend-scope" and check.status == "fail"
        for check in report.checks
    )


def test_describe_scope_check_warns_when_undeclared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no tier declared the same finding is a warning, not a failure."""
    monkeypatch.delenv("GREL_ENVIRONMENT", raising=False)
    micro = Grelmicro(uses=[MemoryProvider()])

    report = micro.describe()

    assert report.ok
    assert any(check.status == "warn" for check in report.checks)


def test_describe_renders_text() -> None:
    """The rendered report names the environment, components, and checks."""
    micro = Grelmicro(
        uses=[Cache(MemoryCacheAdapter())], environment="development"
    )

    text = micro.describe().render()

    assert "Environment: development" in text
    assert "cache/default" in text
    assert "Checks" in text


def test_describe_on_empty_app() -> None:
    """An app with nothing registered still renders."""
    micro = Grelmicro(environment="development")

    text = micro.describe().render()

    assert "none registered" in text


def test_describe_marks_a_bare_provider_defaults() -> None:
    """Each Component a bare Provider registers names that Provider."""
    micro = Grelmicro(uses=[MemoryProvider()], environment="development")

    report = micro.describe()

    assert {c.kind: c.default_of for c in report.components} == {
        "coordination": "memory",
        "cache": "memory",
        "ratelimiter": "memory",
        "circuitbreaker": "memory",
    }


def test_describe_does_not_mark_an_explicit_component() -> None:
    """A Component listed in `uses=` is not a Provider default."""
    micro = Grelmicro(uses=[Cache(MemoryProvider())], environment="development")

    report = micro.describe()

    [cache] = report.components
    assert cache.default_of is None
    assert "default of" not in report.render()


def test_describe_marks_only_the_kinds_a_provider_fills() -> None:
    """An explicit Component wins its kind, the Provider fills the rest."""
    memory = MemoryProvider()
    micro = Grelmicro(uses=[memory, Cache(memory)], environment="development")

    report = micro.describe()

    assert {c.kind: c.default_of for c in report.components} == {
        "cache": None,
        "coordination": "memory",
        "ratelimiter": "memory",
        "circuitbreaker": "memory",
    }


def test_describe_marks_a_provider_used_after_open() -> None:
    """A Provider passed to `micro.use()` late still names its defaults."""
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    micro.describe()

    micro.use(MemoryProvider())

    report = micro.describe()
    assert {c.kind: c.default_of for c in report.components} == {
        "cache": None,
        "coordination": "memory",
        "ratelimiter": "memory",
        "circuitbreaker": "memory",
    }


def test_describe_renders_the_provider_a_default_came_from() -> None:
    """The text report notes which Provider filled each default."""
    memory = MemoryProvider()
    micro = Grelmicro(uses=[memory, Cache(memory)], environment="development")

    lines = micro.describe().render().splitlines()

    cache = next(line for line in lines if "cache/default" in line)
    ratelimiter = next(line for line in lines if "ratelimiter/default" in line)
    assert "(default" not in cache
    assert ratelimiter.endswith("MemoryRateLimiterAdapter (default of memory)")


def test_describe_renders_a_default_on_its_own_provider_once() -> None:
    """A default whose backends borrow its own Provider reads `(provider default)`."""
    micro = Grelmicro(
        uses=[RedisProvider("redis://localhost:6379/0")],
        environment="development",
    )

    lines = micro.describe().render().splitlines()

    cache = next(line for line in lines if "cache/default" in line)
    assert cache.endswith("<- redis (provider default)")


def test_describe_names_the_provider_of_a_default_with_other_backends() -> None:
    """A default whose backends borrow another provider names its own."""
    report = AppReport(
        components=(
            ComponentReport(
                kind="cache",
                name="default",
                component="Cache",
                backends=("RedisCacheAdapter",),
                provider="redis",
                default_of="valkey",
            ),
        ),
    )

    [line] = [
        line for line in report.render().splitlines() if "cache/default" in line
    ]
    assert line.endswith("<- redis (default of valkey)")


def test_load_target_requires_module_attribute_form() -> None:
    """A target without a colon says what to write instead."""
    with pytest.raises(TargetError, match=r"module:attribute"):
        load_target("bogus")


def test_load_target_reports_missing_module() -> None:
    """An unimportable module is reported, not raised as ImportError."""
    with pytest.raises(TargetError, match=r"cannot import module"):
        load_target("grelmicro_does_not_exist:micro")


def test_main_module_raising_at_import_exits_two(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A module that raises while it imports is a usage error naming the error."""
    # Arrange
    (tmp_path / "raising_app.py").write_text(
        "raise TypeError('bad argument')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    # Act
    code = main(["check", "raising_app:micro"])

    # Assert
    assert code == _USAGE_ERROR
    err = capsys.readouterr().err
    assert "importing 'raising_app' raised TypeError: bad argument" in err


def test_load_target_reports_missing_attribute() -> None:
    """A module without the attribute is reported by name."""
    with pytest.raises(TargetError, match=r"has no attribute"):
        load_target("grelmicro:not_a_real_attribute")


def test_load_target_rejects_non_app() -> None:
    """An attribute that is not a `Grelmicro` is refused."""
    with pytest.raises(TargetError, match=r"not a Grelmicro app"):
        load_target("grelmicro:Component")


def test_main_exits_zero_when_checks_pass(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`grelmicro check` prints the report and exits 0 when it is clean."""
    monkeypatch.setenv("GREL_ENVIRONMENT", "development")

    code = main(["check", "tests.test_describe:_PASSING_APP"])

    assert code == 0
    assert "Environment" in capsys.readouterr().out


def test_main_exits_one_when_a_check_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failing check turns into a non-zero exit code for CI."""
    code = main(["check", "tests.test_describe:_FAILING_APP"])

    assert code == 1
    assert "FAIL" in capsys.readouterr().out


def test_main_exits_two_on_bad_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A target that cannot be resolved is a usage error, not a check failure."""
    code = main(["check", "nope"])

    assert code == _USAGE_ERROR
    assert "error:" in capsys.readouterr().err


_PASSING_APP = Grelmicro(uses=[HealthChecks()], environment="development")
_FAILING_APP = Grelmicro(uses=[MemoryProvider()], environment="production")


async def _cli_endpoint(_request: Any) -> JSONResponse:  # noqa: ANN401
    """Return the endpoint the CLI report must enumerate."""
    return JSONResponse({"x": True})


_CLI_CHILD = Starlette(routes=[Route("/x", _cli_endpoint)])
_CLI_APP = FastAPI()
_CLI_APP.mount(
    "/api",
    CORSMiddleware(_CLI_CHILD, allow_origins=["https://client.example"]),
)
_CLI_MICRO = Grelmicro(
    uses=[
        Cache(MemoryCacheAdapter()),
        CachedResponses.from_config(CachedResponsesConfig(include=("/api/*",))),
    ],
    environment="development",
)
_CLI_MICRO.install(_CLI_APP)


async def _report_dependency() -> None:
    """Stand in for a gate the endpoint report must not skip."""


_CLI_POLICY_ROUTER = APIRouter()


@_CLI_POLICY_ROUTER.get(
    "/users/{uid:int}",
    dependencies=[Depends(_report_dependency)],
)
async def _cli_policy_user(uid: int) -> dict[str, int]:
    """Return one typed route for the CLI endpoint table."""
    return {"uid": uid}


_CLI_POLICY_APP = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_CLI_POLICY_MICRO = Grelmicro(
    uses=[
        Cache(MemoryCacheAdapter()),
        CachedResponses.from_config(
            CachedResponsesConfig(include=("/api/users/*",))
        ),
    ],
    environment="development",
)
_CLI_POLICY_MICRO.install(_CLI_POLICY_APP)
_CLI_POLICY_APP.include_router(_CLI_POLICY_ROUTER, prefix="/api")


def test_cli_lists_middleware_wrapped_mounted_endpoints(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The `--app` endpoint table sees routes behind mounted middleware."""
    code = main(
        [
            "check",
            "tests.test_describe:_CLI_MICRO",
            "--app",
            "tests.test_describe:_CLI_APP",
        ]
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "GET    /api/x" in output
    assert "path-patterns" not in output


def test_cli_reports_typed_dependency_route_as_uncached(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`check --app` uses the route template's refusal, not a concrete match."""
    code = main(
        [
            "check",
            "tests.test_describe:_CLI_POLICY_MICRO",
            "--app",
            "tests.test_describe:_CLI_POLICY_APP",
        ]
    )

    output = capsys.readouterr().out
    line = next(
        row for row in output.splitlines() if "/api/users/{uid:int}" in row
    )
    assert code == 0
    assert line.rstrip().endswith("-")


def test_describe_flags_a_forgotten_install() -> None:
    """An app that never called `micro.install(app)` fails the ambient check.

    Without the binding middleware a pattern that omits `backend=` raises
    `OutOfContextError` on the first request. A mounted sub-app that forgot
    the call silently resolves against the host's components instead, which
    nothing else reports.
    """
    micro = Grelmicro(uses=[MemoryProvider()], environment="development")
    app = FastAPI()

    report = micro.describe(app)

    assert not report.ok
    assert any(
        check.name == "ambient-binding" and check.status == "fail"
        for check in report.checks
    )


def test_describe_passes_after_install() -> None:
    """Installing the app clears the ambient check."""
    micro = Grelmicro(uses=[MemoryProvider()], environment="development")
    app = FastAPI()
    micro.install(app)

    report = micro.describe(app)

    assert report.ok


class _AnonymousAuthentication(AuthenticationBackend):
    """Authentication boundary that leaves anonymous requests alone."""

    async def authenticate(self, conn: Any) -> None:  # noqa: ANN401, ARG002
        return None


@pytest.mark.parametrize("framework", ["starlette", "fastapi"])
@pytest.mark.parametrize("middleware", ["cors", "authentication"])
def test_describe_lists_middleware_wrapped_mounted_endpoints(
    framework: str,
    middleware: str,
) -> None:
    """Endpoint reports cross middleware without crossing cache boundaries."""

    async def x(_request: Any = None) -> JSONResponse:  # noqa: ANN401
        return JSONResponse({"x": True})

    if framework == "starlette":
        child: Any = Starlette(routes=[Route("/x", x)])
    else:
        child = FastAPI()
        child.get("/x")(x)
    wrapped = (
        CORSMiddleware(child, allow_origins=["https://client.example"])
        if middleware == "cors"
        else AuthenticationMiddleware(child, backend=_AnonymousAuthentication())
    )
    app = FastAPI()
    app.mount("/api", wrapped)
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(include=("/api/*",)),
        ],
        environment="development",
    )
    micro.install(app)

    report = micro.describe(app)

    endpoint = next(row for row in report.endpoints if row.path == "/api/x")
    assert endpoint.method == "GET"
    assert endpoint.applies == ()
    assert not any(
        check.name == "path-patterns" and "/api/*" in check.detail
        for check in report.checks
    )


def test_describe_cache_declared_behind_a_wrapped_mount_reports_it_cached() -> (
    None
):
    """A route declaring `CachedResponse()` is cached at its route, as reported."""
    child = FastAPI()

    @child.get("/x", dependencies=[CachedResponse()])
    async def x() -> dict[str, bool]:
        return {"x": True}

    app = FastAPI()
    app.mount(
        "/api",
        CORSMiddleware(child, allow_origins=["https://client.example"]),
    )
    micro = Grelmicro(
        uses=[Cache(MemoryCacheAdapter()), CachedResponses()],
        environment="development",
    )
    micro.install(app)

    endpoint = next(
        row for row in micro.describe(app).endpoints if row.path == "/api/x"
    )

    assert endpoint.applies == ("cache 60s",)


def test_describe_does_not_apply_cache_before_an_ordinary_dependency() -> None:
    """The endpoint table preserves the cache's dependency privacy rule."""
    app = FastAPI(dependencies=[CachedResponse()])

    async def audit() -> None:
        return None

    @app.get("/x", dependencies=[Depends(audit)])
    async def x() -> dict[str, bool]:
        return {"x": True}

    micro = Grelmicro(
        uses=[Cache(MemoryCacheAdapter()), CachedResponses()],
        environment="development",
    )
    micro.install(app)

    endpoint = next(
        row for row in micro.describe(app).endpoints if row.path == "/x"
    )

    assert endpoint.applies == ()


def test_describe_reports_cache_on_a_protected_route_declaring_shared() -> None:
    """A protected route declaring `CachedResponse(shared=True)` reports its cache."""
    app = FastAPI(openapi_url=None)

    @app.get(
        "/products",
        dependencies=[
            CachedResponse(ttl=60, shared=True),
            Authenticated(scopes=["r"]),
        ],
    )
    async def products() -> list[str]:
        return []  # pragma: no cover

    micro = Grelmicro(
        uses=[
            AuthenticatedRequests(verifier()),
            Cache(MemoryCacheAdapter()),
            CachedResponses(),
        ],
        environment="development",
    )
    micro.install(app)

    endpoint = next(
        row for row in micro.describe(app).endpoints if row.path == "/products"
    )

    assert endpoint.applies == ("authenticated r", "cache 60s")


def test_dynamic_typed_dependency_report_is_stable_around_traffic() -> None:
    """A late included typed route is uncached before and after a request."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(include=("/api/users/*",)),
        ],
        environment="development",
    )
    micro.install(app)
    router = APIRouter()

    @router.get(
        "/users/{uid:int}",
        dependencies=[Depends(_report_dependency)],
    )
    async def user(uid: int) -> dict[str, int]:
        return {"uid": uid}

    app.include_router(router, prefix="/api")
    before = next(
        row
        for row in micro.describe(app).endpoints
        if row.path == "/api/users/{uid:int}"
    )

    with TestClient(app) as client:
        assert client.get("/api/users/7").json() == {"uid": 7}

    after = next(
        row
        for row in micro.describe(app).endpoints
        if row.path == "/api/users/{uid:int}"
    )

    assert before.applies == after.applies == ()


@pytest.mark.parametrize("key_kind", ["default", "custom"])
def test_describe_idempotency_matches_dependency_gate_policy(
    key_kind: str,
) -> None:
    """Default keys bypass dependency routes; custom identity keys may replay."""
    component = IdempotentRequests(
        key=(lambda scope, key: f"{scope['path']}:{key}")
        if key_kind == "custom"
        else None
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/charge", dependencies=[Depends(_report_dependency)])
    async def charge() -> dict[str, bool]:
        return {"charged": True}

    micro = Grelmicro(
        uses=[Cache(MemoryCacheAdapter()), component],
        environment="development",
    )
    micro.install(app)

    endpoint = next(
        row for row in micro.describe(app).endpoints if row.path == "/charge"
    )

    assert endpoint.applies == (
        ("idempotent 86400s",) if key_kind == "custom" else ()
    )


@pytest.mark.parametrize("key_kind", ["default", "custom"])
def test_describe_idempotency_matches_nested_runtime_gates(
    key_kind: str,
) -> None:
    """Mounted auth and nested dependency gates use runtime's path policy."""
    component = IdempotentRequests(
        key=(lambda scope, key: f"{scope['path']}:{key}")
        if key_kind == "custom"
        else None
    )

    async def post(_request: Any) -> JSONResponse:  # noqa: ANN401
        return JSONResponse({"ok": True})

    authenticated = Starlette(
        routes=[Route("/charge/{item_id:int}", post, methods=["POST"])]
    )
    mounted = AuthenticationMiddleware(
        authenticated,
        backend=_AnonymousAuthentication(),
    )

    dependency_leaf = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @dependency_leaf.post(
        "/nested-dep/{item_id:int}",
        dependencies=[Depends(_report_dependency)],
    )
    async def nested_dep(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    api_key = APIKeyHeader(name="X-API-Key")
    security_leaf = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @security_leaf.post(
        "/nested-security/{item_id:int}",
        dependencies=[Security(api_key)],
    )
    async def nested_security(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    app = Starlette(
        routes=[
            Mount("/auth", app=mounted),
            Route(
                "/nested-dep/{item_id:int}",
                dependency_leaf,
                methods=["POST"],
            ),
            Route(
                "/nested-security/{item_id:int}",
                security_leaf,
                methods=["POST"],
            ),
            Route("/public/{item_id:int}", post, methods=["POST"]),
        ]
    )
    micro = Grelmicro(
        uses=[Cache(MemoryCacheAdapter()), component],
        environment="development",
    )

    endpoints = {row.path: row.applies for row in micro.describe(app).endpoints}
    applied = ("idempotent 86400s",)

    assert endpoints["/public/{item_id:int}"] == applied
    expected_private = applied if key_kind == "custom" else ()
    assert endpoints["/auth/charge/{item_id:int}"] == expected_private
    assert endpoints["/nested-dep/{item_id:int}"] == expected_private
    assert endpoints["/nested-security/{item_id:int}"] == expected_private


@pytest.mark.parametrize(
    ("boundary", "private_path", "public_applies"),
    [
        ("application", "/private/{item_id:int}", ()),
        ("router", "/group/private/{item_id:int}", ("idempotent 86400s",)),
        ("route", "/private/{item_id:int}", ("idempotent 86400s",)),
        ("mount", "/private/{item_id:int}", ("idempotent 86400s",)),
    ],
)
def test_describe_idempotency_matches_authentication_boundaries(
    boundary: str,
    private_path: str,
    public_applies: tuple[str, ...],
) -> None:
    """App, router, route, and mount authentication match runtime bypass."""

    async def post(_request: Any) -> JSONResponse:  # noqa: ANN401
        return JSONResponse({"ok": True})

    private = Route(
        "/private/{item_id:int}",
        post,
        methods=["POST"],
    )
    public = Route("/public/{item_id:int}", post, methods=["POST"])
    auth = Middleware(
        AuthenticationMiddleware,
        backend=_AnonymousAuthentication(),
    )
    if boundary == "application":
        app = Starlette(routes=[private, public], middleware=[auth])
    elif boundary == "router":
        app = Starlette(
            routes=[
                Mount(
                    "/group",
                    app=Router(routes=[private], middleware=[auth]),
                ),
                public,
            ]
        )
    elif boundary == "route":
        app = Starlette(
            routes=[
                Route(
                    "/private/{item_id:int}",
                    post,
                    methods=["POST"],
                    middleware=[auth],
                ),
                public,
            ]
        )
    else:
        child = Starlette(
            routes=[
                Route("/{item_id:int}", post, methods=["POST"]),
            ]
        )
        app = Starlette(
            routes=[
                Mount(
                    "/private",
                    app=AuthenticationMiddleware(
                        child,
                        backend=_AnonymousAuthentication(),
                    ),
                ),
                public,
            ]
        )

    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            IdempotentRequests(),
        ],
        environment="development",
    )
    endpoints = {row.path: row.applies for row in micro.describe(app).endpoints}

    assert endpoints[private_path] == ()
    assert endpoints["/public/{item_id:int}"] == public_applies


def test_describe_idempotency_keeps_route_authentication_method_local() -> None:
    """Reported POST replay remains enabled beside an authenticated GET."""

    async def endpoint(_request: Any) -> JSONResponse:  # noqa: ANN401
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route(
                "/same",
                endpoint,
                methods=["GET"],
                middleware=[
                    Middleware(
                        AuthenticationMiddleware,
                        backend=_AnonymousAuthentication(),
                    )
                ],
            ),
            Route("/same", endpoint, methods=["POST"]),
        ]
    )
    micro = Grelmicro(
        uses=[Cache(MemoryCacheAdapter()), IdempotentRequests()],
        environment="development",
    )

    endpoints = {
        (row.method, row.path): row.applies
        for row in micro.describe(app).endpoints
    }

    assert endpoints[("GET", "/same")] == ()
    assert endpoints[("POST", "/same")] == ("idempotent 86400s",)


def test_describe_idempotency_refreshes_dynamic_auth_topology() -> None:
    """A late protected mount updates the report without hiding its sibling."""

    async def post(_request: Any) -> JSONResponse:  # noqa: ANN401
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/public", post, methods=["POST"])])
    component = IdempotentRequests()
    micro = Grelmicro(
        uses=[Cache(MemoryCacheAdapter()), component],
        environment="development",
    )
    before = {row.path: row.applies for row in micro.describe(app).endpoints}

    private = Starlette(routes=[Route("/charge", post, methods=["POST"])])
    app.router.routes.append(
        Mount(
            "/private",
            app=AuthenticationMiddleware(
                private,
                backend=_AnonymousAuthentication(),
            ),
        )
    )
    after = {row.path: row.applies for row in micro.describe(app).endpoints}

    assert before == {"/public": ("idempotent 86400s",)}
    assert after["/public"] == ("idempotent 86400s",)
    assert after["/private/charge"] == ()


def test_describe_warns_on_unknown_framework() -> None:
    """An unrecognized app is reported, never raised. `describe` only answers."""
    micro = Grelmicro(uses=[MemoryProvider()], environment="development")

    report = micro.describe(object())

    assert report.ok
    assert any(
        check.name == "ambient-binding" and check.status == "warn"
        for check in report.checks
    )


def test_describe_masks_secret_config_fields() -> None:
    """A config field whose name reads as a credential is masked by name."""
    assert _mask("password", "hunter2") == "***"
    assert _mask("api_key", "abc") == "***"
    assert _mask("auth_token", "abc") == "***"
    assert _mask("timeout", _TIMEOUT) == _TIMEOUT


def test_describe_masks_url_shaped_config_values() -> None:
    """A URL in any field is redacted even when the name says nothing."""
    masked = _mask("dsn", "postgres://user:hunter2@localhost:5432/db")

    assert "hunter2" not in masked
    assert "***" in masked


def test_describe_counts_a_failing_factory_as_served() -> None:
    """A factory that fails for its own reasons still ships the adapter.

    Only `NotImplementedError` means "this Provider does not serve the kind".
    A missing pool or bad credentials is a different problem, and reporting
    the kind as declined would send the reader down the wrong path.
    """

    class _Provider(Provider):
        short_name = "explosive"

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def cache_backend(self, **kwargs: Any) -> CacheBackend:  # noqa: ANN401, ARG002
            msg = "no pool yet"
            raise RuntimeError(msg)

    report = describe_provider(_Provider())

    assert "cache" in report.serves
    assert "cache" not in report.declines


def test_integration_resolves_a_framework_subclass() -> None:
    """A user's `FastAPI` subclass still resolves through the framework it extends.

    The lookup walks the app class's MRO, so a subclass declared in the
    application's own package matches on `fastapi` rather than falling
    through to "unsupported framework".
    """

    class _MyApp(FastAPI):
        pass

    integration = load_integration(_MyApp())

    assert integration is not None
    assert getattr(integration, "__name__", "") == (
        "grelmicro.integrations.fastapi"
    )


def test_integration_returns_none_for_unknown_app() -> None:
    """An object no integration claims resolves to `None`, not an exception."""
    assert load_integration(object()) is None


def test_install_works_on_a_framework_subclass() -> None:
    """`install` wires a subclass the same way it wires the base class."""

    class _MyApp(FastAPI):
        pass

    micro = Grelmicro(uses=[MemoryProvider()], environment="development")
    app = _MyApp()
    micro.install(app)

    assert micro.check_ambient_binding(app)


def test_main_puts_the_working_directory_on_the_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`grelmicro check` imports an app from the directory it is run in.

    That is how `uvicorn app:app` behaves, and the app module is usually
    importable only from there.
    """
    # Keep the real entries, so `main` can still import what it loads lazily.
    rest = [entry for entry in sys.path if entry != ""]
    monkeypatch.setattr(sys, "path", ["/somewhere", *rest])

    code = main(["check", "tests.test_describe:_PASSING_APP"])

    assert code == 0
    assert sys.path[0] == ""


def test_main_does_not_duplicate_the_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path already carrying the working directory is left alone.

    Both sides are pinned here rather than left to whichever test happened to
    call `main` first, which under `pytest-xdist` depends on the worker.
    """
    # Keep the real entries, so `main` can still import what it loads lazily.
    rest = [entry for entry in sys.path if entry != ""]
    path = ["/somewhere", "", *rest]
    monkeypatch.setattr(sys, "path", list(path))

    code = main(["check", "tests.test_describe:_PASSING_APP"])

    assert code == 0
    assert sys.path == path


def test_report_types_are_publicly_importable() -> None:
    """A public method must not hand back a type only a private module names.

    `micro.describe()` returns `AppReport`, so a caller has to be able to
    annotate it without reaching into `grelmicro._describe`.
    """
    micro = Grelmicro(environment="development")

    assert type(micro.describe()) is PublicAppReport


def test_report_types_stay_out_of_the_import_path() -> None:
    """Exposing the models must not make `import grelmicro` pay for them."""
    code = "import sys, grelmicro; print('grelmicro._describe' in sys.modules)"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False"


def _counted_reads(app: FastAPI, calls: dict[str, int]) -> None:
    """Add `GET /a` declaring `CachedResponse(ttl=60)`, and `GET /b` declaring none."""

    @app.get("/a", dependencies=[CachedResponse(ttl=60)])
    async def a() -> dict[str, int]:
        calls["a"] = calls.get("a", 0) + 1
        return {"calls": calls["a"]}

    @app.get("/b")
    async def b() -> dict[str, int]:
        calls["b"] = calls.get("b", 0) + 1
        return {"calls": calls["b"]}


def test_describe_cache_under_app_authentication_reports_what_is_cached() -> (
    None
):
    """Checks above a route stop a pattern, not the route's own declaration."""
    # Arrange
    calls: dict[str, int] = {}
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        AuthenticationMiddleware, backend=_AnonymousAuthentication()
    )
    _counted_reads(app, calls)
    micro = Grelmicro(
        uses=[Cache(MemoryCacheAdapter()), CachedResponses(include=("/b",))]
    )
    micro.install(app)

    # Act
    applies = {row.path: row.applies for row in micro.describe(app).endpoints}
    with TestClient(app) as client:
        for path in ("/a", "/a", "/b", "/b"):
            client.get(path)

    # Assert
    assert applies["/a"] == ("cache 60s",)
    assert applies["/b"] == ()
    assert calls == {"a": 1, "b": 2}


def test_describe_cache_declared_under_a_mount_with_middleware_reports_it_cached() -> (
    None
):
    """A route declaring `CachedResponse()` under a mounted app's middleware is cached."""
    # Arrange
    calls: dict[str, int] = {}
    sub = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    sub.add_middleware(CORSMiddleware, allow_origins=["*"])
    _counted_reads(sub, calls)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/sub", sub)
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(include=("/sub/b",)),
        ]
    )
    micro.install(app)

    # Act
    applies = {row.path: row.applies for row in micro.describe(app).endpoints}
    with TestClient(app) as client:
        for path in ("/sub/a", "/sub/a", "/sub/b", "/sub/b"):
            client.get(path)

    # Assert
    assert applies["/sub/a"] == ("cache 60s",)
    assert applies["/sub/b"] == ()
    assert calls == {"a": 1, "b": 2}
