"""What a mounted file may change about the HTTP components while they serve.

The five HTTP components resolve a config the way every other component
does, so a ConfigMap retunes them without a restart. These are the
promises that makes: the values reach the middleware without the stack
being rebuilt, a request answers from one configuration throughout, and
nothing a file says can start caching what the static path would refuse.
"""

from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import Depends, FastAPI
from fastapi.security import APIKeyHeader
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.config import ExternalConfig
from grelmicro.http import (
    CachedResponses,
    CachedResponsesConfig,
    ConditionalRequests,
    ConditionalRequestsConfig,
    ErrorResponses,
    IdempotentRequests,
    IdempotentRequestsConfig,
    RateLimitedRequests,
    RateLimitedRequestsConfig,
)
from grelmicro.integrations.fastapi import CachedResponse
from grelmicro.log import AccessLog, AccessLogConfig
from grelmicro.resilience import RateLimiter
from grelmicro.security import TrustedProxies

pytestmark = pytest.mark.anyio

DEFAULT_TTL = 60.0
"""What the file below says a response is kept for."""

HOT_TTL = 300.0
"""What it says the one hot path is kept for."""

WINDOW = 3600
"""Seconds the file says a stored response replays for."""

MAX_WAIT = 0.5
"""Seconds it says a throttled request waits."""

DECLARED_TTL = 30.0
"""What a component built from a config of its own was given."""

TUPLE_TTL = 45.0
"""What a tuple `include` keeps every path it names for."""

DECLARED_COST = 2
"""Tokens the declared rate limiter spends per request."""

KIND_TTL = 120.0
"""What a kind-wide key retunes every cache to."""

NAMED_TTL = 600.0
"""What the named instance's own key retunes it to instead."""

BUILT = 5
"""How many components the declarative door is swept over."""

YAML = """\
grel:
  cached_responses:
    ttl: 60
    include:
      "/products/*": 60
      "/products/hot": 300
    exclude: ["/admin/*"]
    vary_by_headers: ["accept-language"]
  conditional_requests:
    require_precondition: ["PUT", "DELETE"]
    include: ["/carts/*"]
  idempotent_requests:
    methods: ["POST", "PATCH"]
    include: ["/payments/*"]
  rate_limited_requests:
    max_wait: 0.5
    exclude: ["/livez", "/readyz"]
  access_log:
    exclude: ["/livez"]
  idempotency:
    ttl: 3600
"""


def _limiter(name: str = "burst") -> RateLimiter:
    """Return a limiter with room to spare, for a test that is not metering."""
    return RateLimiter.sliding_window(name, limit=1000, window=60)


def _cached_responses(micro: Grelmicro) -> CachedResponses:
    """Return the app's registered response cache, typed as itself."""
    return cast(
        "CachedResponses",
        next(one for one in micro.components if one.kind == "cached_responses"),
    )


def _mounted(tmp_path: Path, text: str = YAML) -> str:
    """Write a config document and return the path to it."""
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return str(path)


async def test_one_yaml_document_retunes_every_http_component(
    tmp_path: Path,
) -> None:
    """The file an operator writes is the file every component reads."""
    # Arrange
    cache = CachedResponses()
    conditional = ConditionalRequests()
    idempotent = IdempotentRequests()
    rate = RateLimitedRequests(
        _limiter(), trusted=TrustedProxies(["10.0.0.0/8"])
    )
    access = AccessLog()

    # Act
    async with ExternalConfig(_mounted(tmp_path), reload_interval=60):
        # Assert
        assert cache.config.ttl == DEFAULT_TTL
        assert cache.config.vary_by_headers == ("accept-language",)
        assert cache.config.exclude == ("/admin/*",)
        assert conditional.config.require_precondition == ("PUT", "DELETE")
        assert conditional.config.include == ("/carts/*",)
        assert idempotent.config.methods == ("POST", "PATCH")
        assert rate.config.max_wait == MAX_WAIT
        assert rate.config.exclude == ("/livez", "/readyz")
        assert access.config.exclude == ("/livez",)


async def test_a_pattern_keyed_mapping_survives_the_flattening(
    tmp_path: Path,
) -> None:
    """A path is not a variable name, so the mapping travels as one value."""
    # Arrange
    cache = CachedResponses()

    # Act
    async with ExternalConfig(_mounted(tmp_path), reload_interval=60):
        # Assert
        policies = cache._live.state.policies
        assert policies.ttl_for("/products/hot", cache.config.ttl) == HOT_TTL
        assert (
            policies.ttl_for("/products/other", cache.config.ttl) == DEFAULT_TTL
        )
        assert policies.ttl_for("/orders", cache.config.ttl) is None


async def test_the_window_is_tuned_where_the_store_lives(
    tmp_path: Path,
) -> None:
    """The replay window belongs to the `Idempotency`, not to the middleware.

    `IdempotentRequests` builds one named after its namespace, so the
    kind-wide `grel.idempotency.ttl` is what reaches it. The instance's
    own key would be `grel.idempotency.http.ttl`.
    """
    # Arrange
    component = IdempotentRequests()

    # Act
    async with ExternalConfig(_mounted(tmp_path), reload_interval=60):
        # Assert
        assert component.idempotency.config.ttl == WINDOW


async def test_a_reload_reaches_the_middleware_without_rebuilding_it() -> None:
    """A framework will not rebuild its stack once it is serving.

    The middleware reads a cell the component publishes into, so a value
    changed after the app started answering is the one the next request
    is answered with.
    """
    # Arrange
    component = AccessLog()
    _middleware, options = component.asgi_middleware()

    # Act
    await component.reconfigure(
        component.config.model_copy(
            update={
                "exclude": ("/livez",),
            }
        )
    )

    # Assert
    assert options["live"].state.config.exclude == ("/livez",)


async def test_a_live_include_cannot_cache_a_gated_read() -> None:
    """A file must not start caching what the static path refuses.

    `micro.install(app)` refuses a pattern naming a read behind a
    security scheme, because a hit answers before the route's own
    dependencies run. A pattern arriving later has to be refused the
    same way, or live reload opens the hole the static path closes.
    """
    # Arrange
    gate = APIKeyHeader(name="X-Key")
    app = FastAPI()
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])

    @app.get("/private", dependencies=[Depends(gate)])
    async def private() -> dict[str, str]:
        return {"secret": "kept"}

    micro.install(app)
    component = _cached_responses(micro)
    before = component.config

    # Act
    with pytest.raises(TypeError, match="/private"):
        await component.reconfigure(
            CachedResponsesConfig(include={"/private": 60})
        )

    # Assert
    assert component.config is before
    assert component._live.state.policies.ttl_for("/private", 60) is None


async def test_a_bad_live_value_keeps_the_running_config(
    tmp_path: Path,
) -> None:
    """One rejected key never takes the rest of the file down with it."""
    # Arrange
    cache = CachedResponses(ttl=30)
    access = AccessLog()
    document = (
        "grel:\n"
        "  cached_responses:\n"
        "    ttl: 0\n"
        "  access_log:\n"
        '    exclude: ["/livez"]\n'
    )

    # Act
    async with ExternalConfig(_mounted(tmp_path, document), reload_interval=60):
        # Assert
        assert cache.config.ttl == DECLARED_TTL
        assert access.config.exclude == ("/livez",)


async def test_a_new_ttl_answers_the_next_request() -> None:
    """The point of all of it: a hit is kept for what the file now says."""
    # Arrange
    app = FastAPI()
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            ErrorResponses(),
            CachedResponses(ttl=60),
        ]
    )
    calls = 0

    @app.get("/reads", dependencies=[CachedResponse()])
    async def reads() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    micro.install(app)
    component = _cached_responses(micro)

    # Act
    with TestClient(app) as client:
        first = client.get("/reads").json()
        await component.reconfigure(
            CachedResponsesConfig(ttl=60, exclude=("/reads",))
        )
        second = client.get("/reads").json()

    # Assert
    assert first == {"calls": 1}
    assert second == {"calls": 2}


async def test_a_request_answers_from_one_configuration_throughout() -> None:
    """A snapshot is read once, so a swap mid-request cannot be half applied."""
    # Arrange
    component = CachedResponses(
        ttl=DEFAULT_TTL, vary_by_headers=("accept-language",)
    )
    state = component._live.state

    # Act
    await component.reconfigure(
        CachedResponsesConfig(ttl=KIND_TTL, vary_by_headers=())
    )

    # Assert: the snapshot the request took still says what it said.
    assert state.config.ttl == DEFAULT_TTL
    assert state.vary_by_headers == ("accept-language",)
    assert component._live.state.vary_by_headers == ()


async def test_the_endpoint_report_says_what_happens_to_one_route() -> None:
    """One line per endpoint, computed from the one source of truth."""
    # Arrange
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            ErrorResponses(),
            AccessLog(exclude=("/livez",)),
            CachedResponses(include={"/products/*": 60}),
            IdempotentRequests(include=("/orders/*",)),
            RateLimitedRequests(
                _limiter(),
                trusted=TrustedProxies(["10.0.0.0/8"]),
                exclude=("/livez",),
            ),
        ]
    )

    @app.get("/products/list")
    async def products() -> list[str]:
        return []

    @app.post("/orders")
    async def order() -> dict[str, str]:
        return {}

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {}

    micro.install(app)

    # Act
    report = micro.describe(app)
    rows = {(row.method, row.path): row.applies for row in report.endpoints}

    # Assert
    assert rows[("GET", "/products/list")] == (
        "access-log",
        "cache 60s",
        "rate-limit burst",
    )
    assert rows[("POST", "/orders")] == (
        "access-log",
        "idempotent 86400s",
        "rate-limit burst",
    )
    assert rows[("GET", "/livez")] == ()


async def test_the_endpoint_report_is_empty_without_an_app() -> None:
    """The routes are read off the application, so it has to be given one."""
    # Arrange
    micro = Grelmicro(uses=[AccessLog()])

    # Act
    report = micro.describe()

    # Assert
    assert report.endpoints == ()


def test_the_rendered_report_carries_the_endpoint_table() -> None:
    """`python -m grelmicro check --app` prints what a reader asked for."""
    # Arrange
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    micro = Grelmicro(uses=[AccessLog()])

    @app.get("/products")
    async def products() -> list[str]:
        return []

    micro.install(app)

    # Act
    rendered = micro.describe(app).render()

    # Assert
    assert "Endpoints" in rendered
    assert "GET    /products" in rendered
    assert "access-log" in rendered


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param(
            'grel:\n  access_log:\n    exclude: ["/a", "/b"]\n',
            ("/a", "/b"),
            id="json-list",
        ),
        pytest.param(
            "grel:\n  access_log:\n    exclude: /a, /b\n",
            ("/a", "/b"),
            id="comma-separated",
        ),
    ],
)
async def test_a_set_of_paths_reads_the_way_it_is_written(
    tmp_path: Path,
    document: str,
    expected: tuple[str, ...],
) -> None:
    """A file writes a list, and an operator types a comma-separated one."""
    # Arrange
    access = AccessLog()

    # Act
    async with ExternalConfig(_mounted(tmp_path, document), reload_interval=60):
        # Assert
        assert access.config.exclude == expected


async def test_a_kind_wide_key_retunes_every_named_instance(
    tmp_path: Path,
) -> None:
    """Reload follows the precedence construction follows, or they disagree."""
    # Arrange
    default = CachedResponses()
    named = CachedResponses(name="catalog")
    document = (
        "grel:\n"
        "  cached_responses:\n"
        "    ttl: 120\n"
        "    catalog:\n"
        "      ttl: 600\n"
    )

    # Act
    async with ExternalConfig(_mounted(tmp_path, document), reload_interval=60):
        # Assert
        assert default.config.ttl == KIND_TTL
        assert named.config.ttl == NAMED_TTL


def test_the_config_of_every_http_component_is_frozen() -> None:
    """A snapshot a request holds must not change under it."""
    # Arrange
    configs: list[Any] = [
        CachedResponses().config,
        ConditionalRequests().config,
        IdempotentRequests().config,
        AccessLog().config,
        RateLimitedRequests(
            _limiter(), trusted=TrustedProxies(["10.0.0.0/8"])
        ).config,
    ]

    # Act / Assert
    assert len(configs) == BUILT
    for config in configs:
        assert config.model_config["frozen"] is True
        with pytest.raises(ValueError, match="frozen"):
            config.exclude = ("/changed",)


# --- The declarative door -------------------------------------------------


def test_from_config_is_the_one_declarative_door() -> None:
    """What you pass is what runs, and nothing registers for live reload."""
    # Act
    cache = CachedResponses.from_config(CachedResponsesConfig(ttl=DECLARED_TTL))
    conditional = ConditionalRequests.from_config(
        ConditionalRequestsConfig(include=("/carts/*",))
    )
    idempotent = IdempotentRequests.from_config(
        IdempotentRequestsConfig(methods=("PUT",))
    )
    access = AccessLog.from_config(AccessLogConfig(exclude=("/livez",)))
    rate = RateLimitedRequests.from_config(
        RateLimitedRequestsConfig(cost=DECLARED_COST),
        _limiter("declared"),
        trusted=TrustedProxies(["10.0.0.0/8"]),
    )

    # Assert
    assert cache.config.ttl == DECLARED_TTL
    assert conditional.config.include == ("/carts/*",)
    assert idempotent.config.methods == ("PUT",)
    assert access.config.exclude == ("/livez",)
    assert rate.config.cost == DECLARED_COST
    assert rate.limiters[0].name == "declared"
    # None of them reads the environment or joins the reload registry.
    built: list[Any] = [cache, conditional, idempotent, access, rate]
    assert len(built) == BUILT
    for one in built:
        assert one.name == "default"
        assert one._env_prefix is None


async def test_a_declared_component_ignores_a_mounted_file(
    tmp_path: Path,
) -> None:
    """The config-is-truth lane stays on the config it was built with."""
    # Arrange
    declared = AccessLog.from_config(AccessLogConfig(exclude=("/kept",)))

    # Act
    async with ExternalConfig(_mounted(tmp_path), reload_interval=60):
        # Assert
        assert declared.config.exclude == ("/kept",)


# --- What a mounted value that cannot be read does ------------------------


async def test_malformed_json_is_reported_by_the_field(
    tmp_path: Path,
) -> None:
    """The model names the field, and the value stays out of the message."""
    # Arrange
    access = AccessLog(exclude=("/kept",))
    document = 'GREL_ACCESS_LOG_EXCLUDE=["/livez"\n'
    path = tmp_path / "config.env"
    path.write_text(document)

    # Act
    async with ExternalConfig(str(path), reload_interval=60):
        # Assert
        assert access.config.exclude == ("/kept",)


# --- The endpoint report, for the rest of the family ----------------------


def test_the_report_says_which_writes_must_carry_a_precondition() -> None:
    """A `428` is what a caller gets, so the table has to say so."""
    # Arrange
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            ConditionalRequests(
                require_precondition=("PUT",), include=("/carts/*",)
            ),
        ]
    )

    @app.put("/carts/{cart}")
    async def replace(cart: str) -> dict[str, str]:
        return {"cart": cart}

    @app.get("/carts/{cart}")
    async def read(cart: str) -> dict[str, str]:
        return {"cart": cart}

    @app.get("/other")
    async def other() -> dict[str, str]:
        return {}

    micro.install(app)

    # Act
    rows = {
        (row.method, row.path): row.applies
        for row in micro.describe(app).endpoints
    }

    # Assert
    assert rows[("PUT", "/carts/{cart}")] == ("conditional required",)
    assert rows[("GET", "/carts/{cart}")] == ("conditional",)
    assert rows[("GET", "/other")] == ()


def test_a_quiet_path_is_reported_as_quiet() -> None:
    """A probe is logged at debug, which is not the same as not logged."""
    # Arrange
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    micro = Grelmicro(uses=[AccessLog(quiet=("/livez",))])

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {}

    micro.install(app)

    # Act
    rows = {
        (row.method, row.path): row.applies
        for row in micro.describe(app).endpoints
    }

    # Assert
    assert rows[("GET", "/livez")] == ("access-log quiet",)


def test_a_component_that_acts_on_no_endpoint_is_left_out() -> None:
    """Only what answers per endpoint belongs in a per-endpoint view."""
    # Arrange
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    micro = Grelmicro(uses=[ErrorResponses()])

    @app.get("/reads")
    async def reads() -> dict[str, str]:
        return {}

    micro.install(app)

    # Act
    report = micro.describe(app)

    # Assert: nothing reads per endpoint, so there is no table at all.
    assert report.endpoints == ()
    assert "Endpoints" not in report.render()


# --- The cache selector takes both shapes ---------------------------------


def test_a_tuple_include_keeps_every_path_for_the_component_ttl() -> None:
    """The shape every other middleware takes, for the one with a value."""
    # Arrange
    component = CachedResponses(
        ttl=TUPLE_TTL, include=("/products/*", "/catalog")
    )

    # Act
    policies = component._live.state.policies

    # Assert
    assert policies.ttl_for("/products/list", TUPLE_TTL) == TUPLE_TTL
    assert policies.ttl_for("/catalog", TUPLE_TTL) == TUPLE_TTL
    assert policies.ttl_for("/orders", TUPLE_TTL) is None


def test_a_head_route_is_not_a_row_of_its_own() -> None:
    """A `HEAD` reads what a `GET` answers, so the `GET` row is the answer."""

    # Arrange
    async def reads(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({})

    app = Starlette(routes=[Route("/reads", reads, methods=["GET", "HEAD"])])
    micro = Grelmicro(uses=[AccessLog()])
    micro.install(app)

    # Act
    rows = [(row.method, row.path) for row in micro.describe(app).endpoints]

    # Assert
    assert rows == [("GET", "/reads")]
