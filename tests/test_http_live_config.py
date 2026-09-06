"""What a mounted file may change about the HTTP components while they serve.

The five HTTP components resolve a config the way every other component
does, so a ConfigMap retunes them without a restart. These are the
promises that makes: the values reach the middleware without the stack
being rebuilt, a request answers from one configuration throughout, and
nothing a file says can start caching what the static path would refuse.
"""

from collections.abc import Callable
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
from grelmicro._config import _is_container
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.config import ExternalConfig
from grelmicro.errors import SettingsValidationError
from grelmicro.http import (
    CachedResponses,
    CachedResponsesConfig,
    ConditionalRequests,
    ConditionalRequestsConfig,
    ConditionalRequestsMiddleware,
    ErrorResponses,
    IdempotencyMiddleware,
    IdempotentRequests,
    IdempotentRequestsConfig,
    ProblemDetail,
    RateLimitedRequests,
    RateLimitedRequestsConfig,
)
from grelmicro.idempotency import Idempotency
from grelmicro.integrations.fastapi import (
    CachedResponse,
    _annotate_rate_limited,
)
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

BODY_LIMIT = 2048
"""Bytes the file says a body is held to."""

RAISED_BODY_LIMIT = 4096
"""Bytes a cost knob beside a refused key still moves to."""

WAIT = 5.0
"""Seconds the file says a duplicate waits."""

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
    max_body_size: 2048
  idempotent_requests:
    wait_timeout: 5
  rate_limited_requests:
    max_wait: 0.5
    exclude: ["/livez", "/readyz"]
  access_log:
    exclude: ["/livez"]
  idempotency:
    ttl: 3600
"""


async def _nothing(scope: object, receive: object, send: object) -> None:
    """Stand in for the app a hand-wired middleware would wrap."""


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
        assert conditional.config.max_body_size == BODY_LIMIT
        assert idempotent.config.wait_timeout == WAIT
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
            'grel:\n  access_log:\n    exclude: \'["/a", "/b"]\'\n',
            ("/a", "/b"),
            id="json-in-a-scalar",
        ),
    ],
)
async def test_a_set_of_paths_reads_the_way_it_is_written(
    tmp_path: Path,
    document: str,
    expected: tuple[str, ...],
) -> None:
    """A file writes a list, and a flat key writes the JSON one.

    The same shape either way, because pydantic-settings JSON-decodes a
    complex field from the environment and the mounted door has to
    answer the same: one ConfigMap says one thing whether it is mounted
    as a volume or read through `envFrom`.
    """
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


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: IdempotentRequests(methods=cast("Any", "POST")),
            id="idempotent-methods",
        ),
        pytest.param(
            lambda: ConditionalRequests(
                require_precondition=cast("Any", "PUT")
            ),
            id="conditional-require-precondition",
        ),
    ],
)
def test_a_bare_method_is_refused_in_its_own_words(
    build: Callable[[], object],
) -> None:
    """`methods="POST"` reads as four letters, none of which is a method.

    The same mistake as a bare path pattern, said in the words of the
    field it happened on, so a reader is not sent looking for a path.
    """
    # Act / Assert
    with pytest.raises(SettingsValidationError, match="set of HTTP methods"):
        build()


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: IdempotencyMiddleware(
                _nothing,
                idempotency=Idempotency("test"),
                methods=cast("Any", "POST"),
            ),
            id="idempotency-middleware",
        ),
        pytest.param(
            lambda: ConditionalRequestsMiddleware(
                _nothing, require_precondition=cast("Any", "PUT")
            ),
            id="conditional-middleware",
        ),
    ],
)
def test_the_middleware_refuses_a_bare_method_too(
    build: Callable[[], object],
) -> None:
    """`tuple("POST")` is four one-letter methods, and matches nothing.

    Coercing the string would be worse than refusing it: the middleware
    would meter nothing while reporting that it does.
    """
    # Act / Assert
    with pytest.raises(TypeError, match="set of HTTP methods"):
        build()


async def test_the_schema_is_fixed_at_startup() -> None:
    """A published contract is not something a mounted file rewrites.

    Each replica polls its own source on its own clock, so a live schema
    would have two pods behind one load balancer publishing two
    different documents. The schema describes the app as installed, and
    what it states is not live either, so the two cannot disagree.
    """
    # Arrange
    app = FastAPI()
    micro = Grelmicro(uses=[ErrorResponses(), ConditionalRequests()])

    @app.put("/carts/{cart}")
    async def replace(cart: str) -> dict[str, str]:
        return {"cart": cart}

    micro.install(app)
    component = cast(
        "ConditionalRequests",
        next(
            one
            for one in micro.components
            if one.kind == "conditional_requests"
        ),
    )

    # Act
    before = _if_match(app)
    await component.reconfigure(
        ConditionalRequestsConfig(
            require_precondition=("PUT",), include=("/carts/*",)
        )
    )
    after = _if_match(app)

    # Assert: the published document is the one the app was installed
    # with, whatever a caller does to the component afterwards.
    assert before == after
    assert before["required"] is False


def _if_match(app: FastAPI) -> dict[str, Any]:
    """Return the `If-Match` parameter the schema documents on the write."""
    operation = app.openapi()["paths"]["/carts/{cart}"]["put"]
    return next(
        parameter
        for parameter in operation["parameters"]
        if parameter["name"] == "If-Match"
    )


def test_every_container_field_is_decoded_from_a_mounted_value() -> None:
    """A field pydantic cannot build from a string has to be decoded first.

    An `Annotated` type inside a union is the one shape that reads as a
    scalar unless it is unwrapped, and a field skipped here takes the
    whole instance down with it: `reconfigure_all` drops every co-located
    key in the same file when one of them fails.
    """
    # Arrange
    configs = [
        CachedResponsesConfig,
        ConditionalRequestsConfig,
        IdempotentRequestsConfig,
        RateLimitedRequestsConfig,
        AccessLogConfig,
    ]
    swept = [
        (config.__name__, name, field.annotation)
        for config in configs
        for name, field in config.model_fields.items()
    ]

    # Act
    missed = [
        (owner, name)
        for owner, name, annotation in swept
        if _looks_plural(annotation) and not _is_container(annotation)
    ]

    # Assert
    assert swept, "the sweep found no fields to check"
    assert missed == []


def _looks_plural(annotation: object) -> bool:
    """Return whether this annotation names more than one value.

    Read off the string, not off `_is_container`, so the sweep cannot
    agree with the helper it is checking.
    """
    text = str(annotation)
    return "tuple[" in text or "Mapping[" in text


async def test_a_mounted_sequence_applies_beside_its_neighbours(
    tmp_path: Path,
) -> None:
    """One field the decoder skipped used to drop the whole file's patch."""
    # Arrange
    cache = CachedResponses()
    document = (
        "grel:\n"
        "  cached_responses:\n"
        "    ttl: 120\n"
        '    vary_by_query: ["page", "size"]\n'
    )

    # Act
    async with ExternalConfig(_mounted(tmp_path, document), reload_interval=60):
        # Assert
        assert cache.config.vary_by_query == ("page", "size")
        assert cache.config.ttl == KIND_TTL


@pytest.mark.parametrize(
    ("component", "field"),
    [
        pytest.param(
            ConditionalRequests(), "require_precondition", id="precondition"
        ),
        pytest.param(IdempotentRequests(), "require_key", id="require-key"),
        pytest.param(IdempotentRequests(), "key_header", id="key-header"),
    ],
)
async def test_what_the_schema_states_is_not_live(
    component: Any,  # noqa: ANN401
    field: str,
    tmp_path: Path,
) -> None:
    """A file must not change what a client has to send.

    The schema is built once, so a field it states cannot move under it.
    The key is reported to the operator rather than dropped in silence,
    and every other key in the same file still applies.
    """
    # Arrange
    prefix = f"GREL_{component.kind.upper()}_"
    path = tmp_path / "config.env"
    path.write_text(
        f"{prefix}{field.upper()}=true\n"
        f"{prefix}MAX_BODY_SIZE={RAISED_BODY_LIMIT}\n"
    )
    before = getattr(component.config, field)

    # Act
    async with ExternalConfig(str(path), reload_interval=60):
        # Assert: refused, and the cost knob beside it still applies.
        assert getattr(component.config, field) == before
        assert component.config.max_body_size == RAISED_BODY_LIMIT


@pytest.mark.parametrize(
    ("component", "field", "value"),
    [
        pytest.param(
            ConditionalRequests(), "exclude", '["/legacy"]', id="conditional"
        ),
        pytest.param(
            IdempotentRequests(), "exclude", '["/pay"]', id="idempotent"
        ),
        pytest.param(IdempotentRequests(), "methods", '["PUT"]', id="methods"),
    ],
)
async def test_what_protects_a_client_is_wired_in_code(
    component: Any,  # noqa: ANN401
    field: str,
    value: str,
    tmp_path: Path,
) -> None:
    """Live reload tunes what a request costs, never what protects it.

    Take a path out of idempotency and the next retry runs the operation
    twice. Take one out of conditional requests and an unconditional
    write erases an update nobody is told about. Both are changed by a
    deploy, where they are reviewed.
    """
    # Arrange
    prefix = f"GREL_{component.kind.upper()}_"
    path = tmp_path / "config.env"
    path.write_text(
        f"{prefix}{field.upper()}={value}\n{prefix}MAX_BODY_SIZE=4096\n"
    )
    before = getattr(component.config, field)

    # Act
    async with ExternalConfig(str(path), reload_interval=60):
        # Assert: refused, and the cost knob beside it still applies.
        assert getattr(component.config, field) == before
        assert component.config.max_body_size == RAISED_BODY_LIMIT


async def test_what_only_costs_time_is_tuned_live(tmp_path: Path) -> None:
    """A cache miss runs the handler, so turning one off is safe to do live."""
    # Arrange
    cache = CachedResponses(include=("/products/*",))
    path = tmp_path / "config.env"
    path.write_text('GREL_CACHED_RESPONSES_EXCLUDE=["/products/hot"]\n')

    # Act
    async with ExternalConfig(str(path), reload_interval=60):
        # Assert
        assert cache.config.exclude == ("/products/hot",)


def test_the_schema_documents_the_refusal_on_every_operation() -> None:
    """A `429` is a superset, so it stays true whichever paths are metered.

    Which paths are metered is live, and the schema is built once, so
    naming the metered set would publish a document that stops being true
    the first time an operator narrows it. A `429` says only what a
    client may be answered with, never what it must send, so stating it
    everywhere is the form that survives.
    """
    # Arrange
    app = FastAPI(docs_url=None, redoc_url=None)
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                _limiter(),
                trusted=TrustedProxies(["10.0.0.0/8"]),
                exclude=("/livez",),
            ),
        ]
    )

    @app.get("/products")
    async def products() -> list[str]:
        return []

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {}

    micro.install(app)

    # Act
    paths = app.openapi()["paths"]

    # Assert: the excluded path carries it too, because what is excluded
    # is tuned live and the schema is not.
    for path in ("/products", "/livez"):
        responses = paths[path]["get"]["responses"]
        assert "429" in responses
        assert sorted(responses["429"]["headers"]) == [
            "RateLimit",
            "RateLimit-Policy",
            "Retry-After",
        ]


def test_the_refusal_is_left_out_when_the_service_says_so() -> None:
    """A service that publishes its own schema keeps it untouched."""
    # Arrange
    app = FastAPI(docs_url=None, redoc_url=None)
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                _limiter(),
                trusted=TrustedProxies(["10.0.0.0/8"]),
                openapi=False,
            ),
        ]
    )

    @app.get("/products")
    async def products() -> list[str]:
        return []

    micro.install(app)

    # Act
    responses = app.openapi()["paths"]["/products"]["get"]["responses"]

    # Assert
    assert "429" not in responses


def test_a_path_item_that_is_not_an_operation_is_left_alone() -> None:
    """A path item carries more than operations, and only those take a `429`.

    OpenAPI lets a path item hold `summary`, `description` and a shared
    `parameters` list beside its operations. Writing a response into one
    of those would publish a document no client can read.
    """
    # Arrange
    schema: dict[str, Any] = {
        "paths": {
            "/products": {
                "summary": "The catalog",
                "parameters": [{"name": "trace", "in": "header"}],
                "get": {"responses": {"200": {"description": "ok"}}},
            }
        }
    }

    # Act
    _annotate_rate_limited(schema, "application/problem+json", ProblemDetail)

    # Assert
    item: dict[str, Any] = schema["paths"]["/products"]
    assert item["summary"] == "The catalog"
    assert item["parameters"] == [{"name": "trace", "in": "header"}]
    operation: dict[str, Any] = item["get"]
    assert "429" in operation["responses"]


async def test_a_mounted_value_that_is_not_json_reaches_the_field(
    tmp_path: Path,
) -> None:
    """The decoder does not guess, so the field says what it takes.

    A complex value is JSON, the same as it is from the environment.
    Anything else arrives as the string it was, and the field refuses it
    with the message that says what to write instead.
    """
    # Arrange
    access = AccessLog(exclude=("/kept",))
    path = tmp_path / "config.env"
    path.write_text(
        "GREL_ACCESS_LOG_EXCLUDE=/livez\nGREL_ACCESS_LOG_QUERY=false\n"
    )

    # Act
    async with ExternalConfig(str(path), reload_interval=60):
        # Assert
        assert access.config.exclude == ("/kept",)
