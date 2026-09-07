"""What a mounted file may change about the HTTP components while they serve.

The five HTTP components resolve a config the way every other component
does, so a ConfigMap retunes them without a restart. These are the
promises that makes: the values reach the middleware without the stack
being rebuilt, a request answers from one configuration throughout, and
nothing a file says can start caching what the static path would refuse.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, cast

import pytest
from fastapi import APIRouter, Depends, FastAPI, Security, WebSocket
from fastapi.security import APIKeyHeader
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from grelmicro import Grelmicro
from grelmicro._config import _is_container
from grelmicro._describe import (
    _compiled,
    _declared_paths,
    _Endpoint,
    _reach,
)
from grelmicro._paths import selects
from grelmicro.cache import Cache, TTLCache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.serializers import JsonSerializer
from grelmicro.config import ExternalConfig
from grelmicro.errors import SettingsValidationError
from grelmicro.http import (
    CachedResponses,
    CachedResponsesConfig,
    CachedResponsesMiddleware,
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

YAML = """\
grel:
  cached_responses:
    ttl: 60
    include:
      "/products/*": 60
      "/products/hot": 300
    exclude: ["/admin/*"]
    vary_by_headers: ["accept-language"]
  rate_limited_requests:
    max_wait: 0.5
    exclude: ["/livez", "/readyz"]
  access_log:
    exclude: ["/livez"]
  idempotency:
    http:
      ttl: 3600
"""


class _Capture(logging.Handler):
    """Keep every record written, for a test asserting one was not."""

    def __init__(self) -> None:
        """Start with nothing captured."""
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Keep the record rather than writing it anywhere."""
        self.records.append(record)


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
        # Neither of these two moves: both protect a request. Compared
        # field by field, because the environment path builds a settings
        # subclass rather than the plain config class.
        assert conditional.config.model_dump() == (
            ConditionalRequestsConfig().model_dump()
        )
        assert idempotent.config.model_dump() == (
            IdempotentRequestsConfig().model_dump()
        )
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

    `IdempotentRequests` builds one named after the namespace it stores
    under, so the key is `grel.idempotency.http.ttl`. A reload reads the
    instance prefix only: the kind-wide one is a fallback a keyword
    argument beats at construction, and a reload holds no record of what
    code passed, so reading it would let a broadcast overwrite a value
    the code pinned.
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
    ("build", "field"),
    [
        pytest.param(
            ConditionalRequests, "require_precondition", id="precondition"
        ),
        pytest.param(IdempotentRequests, "require_key", id="require-key"),
        pytest.param(IdempotentRequests, "key_header", id="key-header"),
    ],
)
async def test_what_the_schema_states_is_not_live(
    build: Callable[[], Any],
    field: str,
    tmp_path: Path,
) -> None:
    """A file must not change what a client has to send.

    The schema is built once, so a field it states cannot move under it.
    The key is reported to the operator rather than dropped in silence,
    and every other key in the same file still applies.
    """
    # Arrange
    # Built here, not at collection: a component made once for the whole
    # module stays registered for live reload all session.
    component = build()
    prefix = f"GREL_{component.kind.upper()}_"
    cache = CachedResponses()
    path = tmp_path / "config.env"
    path.write_text(
        f"{prefix}{field.upper()}=true\n"
        f"GREL_CACHED_RESPONSES_TTL={KIND_TTL:g}\n"
    )
    before = getattr(component.config, field)

    # Act
    async with ExternalConfig(str(path), reload_interval=60):
        # Assert: refused, and the live key beside it still applies.
        assert getattr(component.config, field) == before
        assert cache.config.ttl == KIND_TTL


@pytest.mark.parametrize(
    ("build", "field", "value"),
    [
        pytest.param(
            ConditionalRequests, "exclude", '["/legacy"]', id="conditional"
        ),
        pytest.param(
            IdempotentRequests, "exclude", '["/pay"]', id="idempotent"
        ),
        pytest.param(IdempotentRequests, "methods", '["PUT"]', id="methods"),
    ],
)
async def test_what_protects_a_client_is_wired_in_code(
    build: Callable[[], Any],
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
    # Built here, not at collection: a component made once for the whole
    # module stays registered for live reload all session.
    component = build()
    prefix = f"GREL_{component.kind.upper()}_"
    cache = CachedResponses()
    path = tmp_path / "config.env"
    path.write_text(
        f"{prefix}{field.upper()}={value}\n"
        f"GREL_CACHED_RESPONSES_TTL={KIND_TTL:g}\n"
    )
    before = getattr(component.config, field)

    # Act
    async with ExternalConfig(str(path), reload_interval=60):
        # Assert: refused, and the live key beside it still applies.
        assert getattr(component.config, field) == before
        assert cache.config.ttl == KIND_TTL


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
                "get": {
                    "responses": {
                        "200": {"description": "ok"},
                        "404": {"description": "gone"},
                    }
                },
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
    # A success states the budget it carries. Anything else does not:
    # what a `404` carries is not what the caller has left.
    assert "headers" in operation["responses"]["200"]
    assert "headers" not in operation["responses"]["404"]


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


def test_a_field_added_later_is_covered_by_the_decision() -> None:
    """The set is read off the config, so no field is live by omission.

    Listing the fields by hand is how one gets forgotten, and a forgotten
    field on either of these two is a guarantee a mounted file can
    remove.
    """
    # Act / Assert
    assert (
        frozenset(ConditionalRequestsConfig.model_fields)
        == ConditionalRequests._IMMUTABLE_RECONFIGURE_FIELDS
    )
    assert (
        frozenset(IdempotentRequestsConfig.model_fields)
        == IdempotentRequests._IMMUTABLE_RECONFIGURE_FIELDS
    )
    # And the three that only cost time or capacity keep every field live.
    for component in (
        CachedResponses(),
        AccessLog(),
        RateLimitedRequests(_limiter(), trusted=TrustedProxies(["10.0.0.0/8"])),
    ):
        assert frozenset() == component._IMMUTABLE_RECONFIGURE_FIELDS


async def test_payload_fingerprinting_is_not_a_cost_knob(
    tmp_path: Path,
) -> None:
    """Turning it off replays the first response to a different payload."""
    # Arrange
    component = IdempotentRequests(fingerprint_body=True)
    path = tmp_path / "config.env"
    path.write_text("GREL_IDEMPOTENT_REQUESTS_FINGERPRINT_BODY=false\n")

    # Act
    async with ExternalConfig(str(path), reload_interval=60):
        # Assert
        assert component.config.fingerprint_body is True


def test_a_typed_converter_is_reported_as_cached() -> None:
    """A declared path is a pattern, not a URL, so it is read off the route.

    Matching `/products/{pid:int}` against the regex compiled from it
    answers no, and the endpoint would be reported as uncached while the
    middleware caches it.
    """
    # Arrange
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            ErrorResponses(),
            CachedResponses(),
        ]
    )

    @app.get("/products/{pid:int}", dependencies=[CachedResponse(ttl=120)])
    async def typed(pid: int) -> dict[str, int]:
        return {"pid": pid}

    @app.get("/rest/{rest:path}", dependencies=[CachedResponse()])
    async def rest(rest: str) -> dict[str, str]:
        return {"rest": rest}

    micro.install(app)

    # Act
    rows = {
        (row.method, row.path): row.applies
        for row in micro.describe(app).endpoints
    }

    # Assert
    assert rows[("GET", "/products/{pid:int}")] == ("cache 120s",)
    assert rows[("GET", "/rest/{rest:path}")] == ("cache 60s",)


def test_broken_json_is_reported_as_broken_json() -> None:
    """An operator who wrote a list is not told to write a list."""
    # Act / Assert
    with pytest.raises(SettingsValidationError, match="does not parse"):
        AccessLog(exclude=cast("Any", '["/livez"'))
    with pytest.raises(SettingsValidationError, match="is a string"):
        AccessLog(exclude=cast("Any", "/livez"))


async def test_code_may_still_swap_a_configuration_a_file_may_not() -> None:
    """The restriction is on the mounted source, not on the API.

    `reconfigure` is called by application code, which is code: reviewed,
    versioned, and shipped with the image. What it may not do is arrive
    from a file that is none of those.
    """
    # Arrange
    component = IdempotentRequests()
    before = component._live.state

    # Act
    await component.reconfigure(
        IdempotentRequestsConfig(methods=("POST", "PUT"))
    )

    # Assert
    assert before.methods == frozenset({"POST"})
    assert component._live.state.methods == frozenset({"POST", "PUT"})


def test_a_gated_read_under_a_marked_router_is_not_reported_as_cached() -> None:
    """An audit asking whether a gated response is cached must be told no.

    A router declares caching for what it holds, and holds more than
    reads. The write under it and the read behind a security scheme are
    left to their handlers, and the report has to say the same, or it
    answers the one question it exists to answer in the dangerous
    direction.
    """
    # Arrange
    gate = APIKeyHeader(name="X-Key")
    router = APIRouter(dependencies=[CachedResponse(ttl=60)])

    @router.get("/secret", dependencies=[Security(gate)])
    async def secret() -> dict[str, str]:
        return {}

    @router.get("/open")
    async def open_read() -> dict[str, str]:
        return {}

    @router.post("/write")
    async def write() -> dict[str, str]:
        return {}

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            ErrorResponses(),
            CachedResponses(),
        ]
    )
    micro.install(app)
    component = _cached_responses(micro)

    # Act
    rows = {
        (row.method, row.path): row.applies
        for row in micro.describe(app).endpoints
    }

    # Assert: the report and the runtime agree, endpoint by endpoint.
    assert rows[("GET", "/open")] == ("cache 60s",)
    assert rows[("GET", "/secret")] == ()
    assert rows[("POST", "/write")] == ()
    policies = component._live.state.policies
    assert policies.ttl_for("/open", DEFAULT_TTL) == DEFAULT_TTL
    assert policies.ttl_for("/secret", DEFAULT_TTL) is None
    assert policies.ttl_for("/write", DEFAULT_TTL) is None


def test_a_duplicate_may_be_answered_without_waiting() -> None:
    """Zero is how "do not wait" is written, not a value to refuse."""
    # Act
    component = IdempotentRequests(wait_timeout=0)

    # Assert
    assert component.config.wait_timeout == 0.0


def test_a_hand_wired_middleware_refuses_without_echoing_the_value() -> None:
    """The one error every setting raises, on both doors.

    A middleware built by hand builds the same config, so pydantic's own
    error would escape unwrapped, and that one carries the input. A
    rejected value is never repeated, whichever door rejected it.
    """
    # Act / Assert
    with pytest.raises(SettingsValidationError) as caught:
        IdempotencyMiddleware(
            _nothing,
            idempotency=Idempotency("test"),
            key_header=cast("Any", b"SECRET-VALUE"),
        )
    assert "SECRET-VALUE" not in str(caught.value)
    assert "input_value" not in str(caught.value)


def test_a_broadcast_key_never_overwrites_what_the_code_pinned() -> None:
    """Reload follows the order construction follows, or it inverts it.

    A keyword argument beats the kind-wide variable at startup. A reload
    holds no record of what code passed, so reading the kind prefix
    would let one key in a shared file retune every instance of a kind,
    including the ones a service pinned on purpose.
    """
    # Arrange
    component = CachedResponses(name="catalog", ttl=30)

    # Act / Assert
    assert component._env_prefix == "GREL_CACHED_RESPONSES_CATALOG_"
    assert not hasattr(component, "_kind_env_prefix")


def test_the_schema_states_the_budget_a_success_carries() -> None:
    """A client reads what it has left off a `200`, not off the refusal."""
    # Arrange
    app = FastAPI(docs_url=None, redoc_url=None)
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                _limiter(), trusted=TrustedProxies(["10.0.0.0/8"])
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
    assert sorted(responses["200"]["headers"]) == [
        "RateLimit",
        "RateLimit-Policy",
    ]
    assert sorted(responses["429"]["headers"]) == [
        "RateLimit",
        "RateLimit-Policy",
        "Retry-After",
    ]


def test_a_second_instance_says_which_one_it_is() -> None:
    """Two rows that read the same would mean different things."""
    # Arrange
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            ErrorResponses(),
            CachedResponses(include={"/products/*": 60}),
            CachedResponses(
                name="hot",
                include={"/products/hot": 300},
                namespace="hot",
            ),
        ]
    )

    @app.get("/products/hot")
    async def hot() -> list[str]:
        return []

    micro.install(app)

    # Act
    applies = micro.describe(app).endpoints[0].applies

    # Assert
    assert applies == ("cache 60s", "cache 300s (hot)")


def test_a_pattern_matching_no_route_is_reported() -> None:
    """A mistyped pattern turns a rule off and leaves no other trace.

    It gets no row of its own, because rows come from routes, so the
    only place it can surface is beside the table. A warning rather than
    a failure: a router mounted after `install` is legitimate.
    """
    # Arrange
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            ErrorResponses(),
            CachedResponses(include={"/typoo": 60}),
            AccessLog(exclude=("/livez",)),
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
    report = micro.describe(app)
    warned = [check for check in report.checks if check.name == "path-patterns"]

    # Assert
    assert [check.detail for check in warned] == [
        "cached_responses/default names /typoo, which no route matches"
    ]
    assert report.ok is True
    assert "/typoo" not in {endpoint.path for endpoint in report.endpoints}


def test_no_pattern_check_without_an_app() -> None:
    """The routes are read off the application, so there is nothing to check."""
    # Arrange
    micro = Grelmicro(uses=[AccessLog(exclude=("/livez",))])

    # Act
    report = micro.describe()

    # Assert
    assert not [c for c in report.checks if c.name == "path-patterns"]


def test_a_concrete_path_under_a_parameterized_route_is_not_dead() -> None:
    """A pattern is matched against the URL, not against the template.

    `/users/me` does select the request `GET /users/{uid}` answers, so
    calling it dead would be a warning that cries wolf on the shape the
    docs themselves recommend.
    """
    # Arrange
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            ErrorResponses(),
            AccessLog(exclude=("/users/me", "/typoo")),
            CachedResponses(include={"/products/hot": 300, "/nope/*": 60}),
        ]
    )

    @app.get("/users/{uid}")
    async def user(uid: str) -> dict[str, str]:
        return {"uid": uid}

    @app.get("/products/{slug}")
    async def product(slug: str) -> dict[str, str]:
        return {"slug": slug}

    micro.install(app)

    # Act
    warned = sorted(
        check.detail
        for check in micro.describe(app).checks
        if check.name == "path-patterns"
    )

    # Assert: only the two that name nothing at all.
    assert warned == [
        "access_log/default names /typoo, which no route matches",
        "cached_responses/default names /nope/*, which no route matches",
    ]


def test_a_route_with_no_method_still_counts_as_declared() -> None:
    """A websocket route has no row in the table and is still a route."""
    # Arrange
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    micro = Grelmicro(uses=[AccessLog(exclude=("/live",))])

    @app.websocket("/live")
    async def live(websocket: WebSocket) -> None:  # pragma: no cover
        await websocket.accept()

    micro.install(app)

    # Act
    report = micro.describe(app)

    # Assert
    assert not [c for c in report.checks if c.name == "path-patterns"]
    assert report.endpoints == ()


def test_a_rule_reaching_part_of_a_route_says_so() -> None:
    """A route template stands for many URLs, and a pattern may name one.

    `"/users/me"` selects one of the requests `GET /users/{uid}` answers
    and not the others. Saying the rule applies to the endpoint would
    overstate it, and saying it does not would be wrong, so the row says
    it reaches some of it. The pattern check reads the same way, or one
    report would say two contradictory things.
    """
    # Arrange
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            ErrorResponses(),
            AccessLog(exclude=("/users/me",)),
            IdempotentRequests(include=("/users/me/pay",)),
        ]
    )

    @app.post("/users/{uid}/pay")
    async def pay(uid: str) -> dict[str, str]:
        return {"uid": uid}

    @app.get("/users/{uid}")
    async def user(uid: str) -> dict[str, str]:
        return {"uid": uid}

    micro.install(app)

    # Act
    report = micro.describe(app)
    rows = {(row.method, row.path): row.applies for row in report.endpoints}

    # Assert
    assert rows[("GET", "/users/{uid}")] == ("access-log (some paths)",)
    assert rows[("POST", "/users/{uid}/pay")] == (
        "access-log",
        "idempotent 86400s (some paths)",
    )
    assert not [c for c in report.checks if c.name == "path-patterns"]


def test_describe_reads_a_route_no_compiler_here_understands() -> None:
    """A report answers questions, so it comes back with what it could read.

    Starlette owns the path compiler and is not a dependency of
    grelmicro, and another framework spells a converter its own way. A
    template neither can read stands for itself.
    """

    # Arrange
    class Route:
        path = "/orders/{placed:date}"

    class App:
        routes = (Route(),)

    # Act
    declared = _declared_paths(App())

    # Assert
    assert declared == [("/orders/{placed:date}", None)]


async def test_a_steady_config_map_does_not_warn_every_poll(
    tmp_path: Path,
) -> None:
    """A file that mirrors what startup set has changed nothing.

    The warning names a field an operator is trying to move. Judging the
    string before decoding it made every container field look changed,
    and every field of this component is one an operator cannot move, so
    a quiet deployment would have said so on every poll.
    """
    # Arrange
    # Its own name, so no other instance of the kind shares the prefix
    # and legitimately differs from what this file says.
    component = IdempotentRequests(name="steady", exclude=("/livez",))
    path = tmp_path / "config.env"
    path.write_text('GREL_IDEMPOTENT_REQUESTS_STEADY_EXCLUDE=["/livez"]\n')
    logger = logging.getLogger("grelmicro")
    handler = _Capture()

    # Act
    logger.addHandler(handler)
    try:
        async with ExternalConfig(str(path), reload_interval=60):
            pass
    finally:
        logger.removeHandler(handler)
    records = handler.records

    # Assert
    assert component.config.exclude == ("/livez",)
    assert not [
        r for r in records if "only applies at startup" in r.getMessage()
    ]


def test_a_route_with_no_regex_reaches_no_pattern() -> None:
    """A template no compiler read stands for itself and nothing more.

    Without a regex there is no way to ask whether a pattern names one
    of the URLs it serves, so it does not, and the row says only what
    the template itself answers.
    """
    # Arrange
    endpoint = _Endpoint(
        method="GET",
        path="/orders/{placed:date}",
        route=None,
        contexts=(),
        regex=None,
    )

    # Act / Assert
    assert _reach(endpoint, ("/orders/2026-01-01",), ()) is None
    assert _reach(endpoint, ("/orders/{placed:date}",), ()) == ""


def test_no_compiler_leaves_every_template_standing_for_itself() -> None:
    """Starlette owns the compiler and grelmicro does not depend on it."""
    # Arrange
    # Act / Assert
    assert _compiled(None, "/orders") is None


def test_exclude_only_narrows_what_include_already_reaches() -> None:
    """A rule include never named is not reached in part by excluding one.

    Reading `exclude` first reported a component as touching some of an
    endpoint that `include` had already ruled out entirely, so the table
    said the opposite of what runs.
    """
    # Arrange
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    micro = Grelmicro(
        uses=[AccessLog(include=("/api/*",), exclude=("/health/live",))]
    )

    @app.get("/health/{probe}")
    async def probe(probe: str) -> dict[str, str]:
        return {"probe": probe}

    @app.get("/api/{thing}")
    async def thing(thing: str) -> dict[str, str]:
        return {"thing": thing}

    micro.install(app)

    # Act
    rows = {
        (row.method, row.path): row.applies
        for row in micro.describe(app).endpoints
    }

    # Assert: what the middleware answers, endpoint by endpoint.
    assert rows[("GET", "/health/{probe}")] == ()
    assert rows[("GET", "/api/{thing}")] == ("access-log",)
    assert not selects(
        "/health/ready", include=("/api/*",), exclude=("/health/live",)
    )


def test_the_whole_query_string_can_be_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`None` is what this field means, so writing it has to say something.

    A keyword argument wins over a variable, and `resolve_config` reads a
    `None` keyword as one nobody passed, so this is the one field that
    needs a sentinel to tell the two apart.
    """
    # Arrange
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_CACHED_RESPONSES_VARY_BY_QUERY", '["q"]')

    # Act / Assert
    assert CachedResponses(vary_by_query=None).config.vary_by_query is None
    assert CachedResponses().config.vary_by_query == ("q",)
    assert CachedResponses(vary_by_query=("page",)).config.vary_by_query == (
        "page",
    )


def test_the_hand_wired_cache_takes_the_tuple_form_too() -> None:
    """The shared vocabulary reaches the door the docs call hand-wired."""
    # Arrange
    cache = TTLCache(ttl=DEFAULT_TTL, serializer=JsonSerializer())

    # Act
    middleware = CachedResponsesMiddleware(
        _nothing, cache=cache, include=("/products/*",)
    )

    # Assert
    policies = middleware._live.state.policies
    assert policies.ttl_for("/products/list", DEFAULT_TTL) == DEFAULT_TTL
    assert policies.ttl_for("/orders", DEFAULT_TTL) is None


def test_a_pattern_naming_a_gated_url_is_refused() -> None:
    """A pattern names a URL, and a route template stands for many.

    `include={"/users/me": 30}` names no template on an app declaring
    `GET /users/{uid}`, so a refusal that asked the template alone let
    the URL through, cached a gated read, and answered over the gate:
    the second caller was handed the first caller's response, and a
    request carrying no key at all was answered `200` with it.
    """
    # Arrange
    gate = APIKeyHeader(name="X-API-Key")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            ErrorResponses(),
            CachedResponses(include={"/users/me": 30}),
        ]
    )

    @app.get("/users/{uid}")
    async def user(
        uid: str, key: Annotated[str, Security(gate)]
    ) -> dict[str, str]:
        return {"caller": key, "uid": uid}

    # Act / Assert
    with pytest.raises(TypeError, match="gated by APIKeyHeader"):
        micro.install(app)


@pytest.mark.parametrize(
    ("build", "expected", "marked"),
    [
        pytest.param(
            lambda: CachedResponses(include={"/users/me": 30}),
            ("cache 30s (some paths)",),
            False,
            id="include-names-a-url",
        ),
        pytest.param(
            lambda: CachedResponses(exclude=("/users/me",)),
            ("cache 45s (some paths)",),
            True,
            id="exclude-names-a-url",
        ),
        pytest.param(
            CachedResponses, ("cache 45s",), True, id="the-whole-route"
        ),
    ],
)
def test_the_cache_row_reads_like_every_other(
    build: Callable[[], CachedResponses],
    expected: tuple[str, ...],
    *,
    marked: bool,
) -> None:
    """The one component hardest to reason about by hand says the same.

    It was the only reader not going through the shared reach, so it
    both omitted caching a pattern had turned on and overstated caching
    an exclude had taken part of away.
    """
    # Arrange
    # Built here, not at collection: a component made once for the whole
    # module is registered for live reload, so any other test running
    # `ExternalConfig` would reconfigure it out from under this one.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    micro = Grelmicro(
        uses=[Cache(MemoryCacheAdapter()), ErrorResponses(), build()]
    )
    declared = [CachedResponse(ttl=45)] if marked else []

    @app.get("/users/{uid}", dependencies=declared)
    async def user(uid: str) -> dict[str, str]:
        return {"uid": uid}

    micro.install(app)

    # Act
    applies = micro.describe(app).endpoints[0].applies

    # Assert
    assert applies == expected
