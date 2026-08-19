"""The framework's own errors answer in the registered format too.

Registering `ErrorResponses` adopts one format for the whole API. Answering
half of it in that format and half in the framework's own would be the
surprising outcome, so an `HTTPException` a handler raises and a request
that failed validation are reshaped as well.

Only the shape changes. The status, the message and any header the exception
carried are the framework's, and stay. The one exception is validation,
which is classified as a kind of its own so the same failure answers the same
way whichever framework validated it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from fastapi import FastAPI
from fastapi import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from litestar import Litestar, get, post
from litestar import Request as LitestarRequest
from litestar.exceptions import HTTPException as LitestarHTTPException
from litestar.exceptions import NotFoundException, ValidationException
from litestar.openapi.datastructures import ResponseSpec
from litestar.response import Response as LitestarResponse
from litestar.testing import TestClient as LitestarTestClient
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.status import (
    HTTP_200_OK,
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_418_IM_A_TEAPOT,
    HTTP_422_UNPROCESSABLE_CONTENT,
)
from starlette.testclient import TestClient as StarletteTestClient

from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.http import ERROR_DOCS_BASE, PROBLEM_MEDIA_TYPE, ErrorResponses
from grelmicro.http import ProblemDetail as GrelmicroProblem
from grelmicro.idempotency import Idempotency
from grelmicro.integrations.fastapi import (
    IdempotencyMiddleware,
    document_idempotency,
    error_response,
)
from grelmicro.integrations.litestar import _field_errors
from grelmicro.integrations.litestar import (
    error_response as litestar_error_response,
)
from grelmicro.providers.memory import MemoryProvider

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

pytestmark = [pytest.mark.timeout(5)]

VALIDATION_MESSAGE = "bad input"
"""What the framework said, which must reach the client in every format."""

NOT_FOUND_DETAIL = "Charge not found"
"""Message the app puts on its own error, which must survive untouched."""

BALANCE = 30
"""Value a structured detail carries, which must reach the client."""

UNREGISTERED_STATUS = 599
"""A status outside the IANA registry, which a proxy may well answer with."""


class Charge(BaseModel):
    """A body with one required integer, so a string fails validation."""

    amount: int


class GrelmicroProblemDetail(BaseModel):
    """An app model under the qualified name grelmicro falls back to."""

    also_mine: str


class ProblemDetail(BaseModel):
    """An app model that happens to share grelmicro's name.

    Defined at module scope because Litestar resolves a handler's signature
    against its module, and a class defined inside a test is invisible there.
    """

    mine: str


def _fastapi() -> FastAPI:
    """Build a FastAPI app that raises the framework's own errors."""
    app = FastAPI()

    @app.get("/missing")
    async def missing() -> dict[str, str]:
        raise FastAPIHTTPException(HTTP_404_NOT_FOUND, NOT_FOUND_DETAIL)

    @app.get("/auth")
    async def auth() -> dict[str, str]:
        raise FastAPIHTTPException(
            HTTP_401_UNAUTHORIZED,
            "No",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.post("/charge")
    async def charge(body: Charge) -> dict[str, bool]:  # noqa: ARG001
        return {"ok": True}

    return app


def _litestar() -> Litestar:
    """Build a Litestar app that raises the framework's own errors."""

    @get("/missing")
    async def missing() -> dict[str, str]:
        raise NotFoundException(NOT_FOUND_DETAIL)

    @post("/charge")
    async def charge(data: Charge) -> dict[str, bool]:  # noqa: ARG001
        return {"ok": True}

    return Litestar(route_handlers=[missing, charge])


def _starlette() -> Starlette:
    """Build a plain Starlette app that raises `HTTPException`."""

    async def missing(request: Request) -> None:  # noqa: ARG001
        raise StarletteHTTPException(HTTP_404_NOT_FOUND, NOT_FOUND_DETAIL)

    return Starlette(routes=[Route("/missing", missing)])


def _not_found(app: Any) -> tuple[int, str, dict[str, Any]]:  # noqa: ANN401
    """Answer `/missing` on whichever framework `app` belongs to."""
    if isinstance(app, Litestar):
        with LitestarTestClient(
            app=app, raise_server_exceptions=False
        ) as client:
            response = client.get("/missing")
    elif isinstance(app, FastAPI):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/missing")
    else:
        with StarletteTestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/missing")
    return (
        response.status_code,
        response.headers["content-type"],
        response.json(),
    )


def test_every_framework_reshapes_its_own_error_identically() -> None:
    """One format for the whole API means the same answer everywhere."""
    # Arrange
    apps = [_fastapi(), _starlette(), _litestar()]
    for app in apps:
        Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    answers = [_not_found(app) for app in apps]

    # Assert
    assert answers[0] == answers[1] == answers[2]
    status, media_type, body = answers[0]
    assert status == HTTP_404_NOT_FOUND
    assert media_type == PROBLEM_MEDIA_TYPE
    assert body == {
        "type": "about:blank",
        "title": "Not Found",
        "status": HTTP_404_NOT_FOUND,
        "detail": NOT_FOUND_DETAIL,
        "instance": "/missing",
    }


def test_the_framework_error_keeps_its_status_and_message() -> None:
    """Only the shape changes. What the app said is what the client reads."""
    # Arrange
    app = _fastapi()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    status, _, body = _not_found(app)

    # Assert
    assert status == HTTP_404_NOT_FOUND
    assert body["detail"] == NOT_FOUND_DETAIL


def test_a_header_the_app_set_survives() -> None:
    """`WWW-Authenticate` on a 401 is part of the answer, not decoration."""
    # Arrange
    app = _fastapi()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/auth")

    # Assert
    assert response.status_code == HTTP_401_UNAUTHORIZED
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE


@pytest.mark.parametrize(
    ("build", "status"),
    [
        (_fastapi, HTTP_422_UNPROCESSABLE_CONTENT),
        (_litestar, HTTP_400_BAD_REQUEST),
    ],
)
def test_validation_carries_one_identifier_on_every_framework(
    build: Any,  # noqa: ANN401
    status: int,
) -> None:
    """One condition, one identifier, and the framework keeps its status.

    FastAPI answers `422` and Litestar `400`. Which is right is contested,
    RFC 9110 section 15.5.21 defines `422` and Zalando leaves it off its
    common list, and those projects have already answered it for their
    users. grelmicro reshapes an answer rather than overruling it, so a
    client branches on `validation-failed` and reads the same identifier
    either way.
    """
    # Arrange
    app = build()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    if isinstance(app, Litestar):
        with LitestarTestClient(
            app=app, raise_server_exceptions=False
        ) as client:
            response = client.post("/charge", json={"amount": "abc"})
    else:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/charge", json={"amount": "abc"})

    # Assert
    assert response.status_code == status
    body = response.json()
    assert body["type"] == f"{ERROR_DOCS_BASE}#validation-failed"
    assert body["status"] == status
    assert body["errors"]


def test_the_validation_input_is_not_echoed_back() -> None:
    """FastAPI includes what the client sent. It adds nothing, so it goes."""
    # Arrange
    app = _fastapi()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/charge", json={"amount": "abc"})

    # Assert
    assert "input" not in json.dumps(response.json())
    assert all(
        set(entry) <= {"loc", "msg", "type"}
        for entry in response.json()["errors"]
    )


def test_a_handler_you_registered_keeps_the_framework_error() -> None:
    """Registering your own is how one error opts back out of the format."""
    # Arrange
    app = _fastapi()

    async def mine(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({"mine": True}, status_code=HTTP_418_IM_A_TEAPOT)

    app.add_exception_handler(StarletteHTTPException, mine)
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/missing")

    # Assert
    assert response.status_code == HTTP_418_IM_A_TEAPOT
    assert response.json() == {"mine": True}


def test_without_the_component_the_framework_answers_as_it_always_did() -> None:
    """The opt-in governs the framework's errors as well as grelmicro's."""
    # Arrange
    app = _fastapi()
    Grelmicro(uses=[]).install(app)

    # Act
    status, media_type, body = _not_found(app)

    # Assert
    assert status == HTTP_404_NOT_FOUND
    assert media_type == "application/json"
    assert body == {"detail": NOT_FOUND_DETAIL}


# --- Your own handler ---------------------------------------------------


class InsufficientFundsError(Exception):
    """An error of the app's own, with nothing to do with grelmicro."""


def test_your_own_handler_can_answer_in_the_app_format() -> None:
    """Writing a handler should not mean leaving the shared shape behind."""
    # Arrange
    app = FastAPI()

    @app.get("/charge")
    async def charge() -> dict[str, str]:
        raise InsufficientFundsError

    async def handle(request: Request, exc: Exception) -> Response:  # noqa: ARG001
        return error_response(
            request,
            status=HTTP_409_CONFLICT,
            detail="The account does not hold enough to cover this charge.",
            extensions={"balance": 30},
        )

    app.add_exception_handler(InsufficientFundsError, handle)
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/charge")

    # Assert
    assert response.status_code == HTTP_409_CONFLICT
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json() == {
        "type": "about:blank",
        "title": "Conflict",
        "status": HTTP_409_CONFLICT,
        "detail": "The account does not hold enough to cover this charge.",
        "instance": "/charge",
        "balance": 30,
    }


def test_your_own_handler_follows_the_registered_format() -> None:
    """A TMF service answers in TMF from your handler too, with no second place."""
    # Arrange
    app = FastAPI()

    @app.get("/charge")
    async def charge() -> dict[str, str]:
        raise InsufficientFundsError

    async def handle(request: Request, exc: Exception) -> Response:  # noqa: ARG001
        return error_response(request, status=HTTP_409_CONFLICT, detail="Nope")

    app.add_exception_handler(InsufficientFundsError, handle)
    Grelmicro(uses=[ErrorResponses.tmf(code_prefix="SBB")]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/charge")

    # Assert
    assert response.headers["content-type"] == "application/json"
    assert response.json()["code"] == str(HTTP_409_CONFLICT)
    assert response.json()["reason"] == "Conflict"


def test_your_own_handler_works_without_a_registered_format() -> None:
    """An app that registered nothing still gets a well-formed body."""
    # Arrange
    app = FastAPI()

    @app.get("/charge")
    async def charge() -> dict[str, str]:
        raise InsufficientFundsError

    async def handle(request: Request, exc: Exception) -> Response:  # noqa: ARG001
        return error_response(request, status=HTTP_409_CONFLICT)

    app.add_exception_handler(InsufficientFundsError, handle)
    Grelmicro(uses=[]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/charge")

    # Assert
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json()["title"] == "Conflict"


def test_litestar_own_handler_answers_in_the_app_format() -> None:
    """The same helper exists for Litestar, reading the same registration."""

    # Arrange
    @get("/charge")
    async def charge() -> dict[str, str]:
        raise InsufficientFundsError

    def handle(
        request: LitestarRequest,
        exc: Exception,  # noqa: ARG001
    ) -> LitestarResponse:
        return litestar_error_response(
            request, status=HTTP_409_CONFLICT, detail="Nope"
        )

    app = Litestar(
        route_handlers=[charge],
        exception_handlers={InsufficientFundsError: handle},
    )
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with LitestarTestClient(app=app, raise_server_exceptions=False) as client:
        response = client.get("/charge")

    # Assert
    assert response.status_code == HTTP_409_CONFLICT
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json()["detail"] == "Nope"


def test_the_generated_validation_response_is_republished() -> None:
    """A client generated from the schema decodes what the app now answers."""
    # Arrange
    app = _fastapi()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    content = app.openapi()["paths"]["/charge"]["post"]["responses"]["422"][
        "content"
    ]

    # Assert
    assert content == {
        PROBLEM_MEDIA_TYPE: {
            "schema": {"$ref": "#/components/schemas/ProblemDetail"}
        }
    }


def test_a_validation_response_the_app_declared_is_left_alone() -> None:
    """Only the entry FastAPI generated is rewritten. Yours is yours."""
    # Arrange
    app = FastAPI()

    @app.post(
        "/charge", responses={422: {"description": "Mine", "content": {}}}
    )
    async def charge(body: Charge) -> dict[str, bool]:  # noqa: ARG001
        return {"ok": True}

    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    response = app.openapi()["paths"]["/charge"]["post"]["responses"]["422"]

    # Assert
    assert response["description"] == "Mine"


def test_an_operation_with_no_validation_response_is_untouched() -> None:
    """A route that validates nothing has no entry to republish."""
    # Arrange
    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    responses = app.openapi()["paths"]["/ping"]["get"]["responses"]

    # Assert
    assert set(responses) == {"200"}


def test_litestar_keeps_a_handler_you_passed_for_its_own_error() -> None:
    """`Litestar(exception_handlers=...)` wins for the framework error too."""

    # Arrange
    @get("/missing")
    async def missing() -> dict[str, str]:
        raise NotFoundException(NOT_FOUND_DETAIL)

    def mine(
        request: LitestarRequest,  # noqa: ARG001
        exc: Exception,  # noqa: ARG001
    ) -> LitestarResponse:
        return LitestarResponse(content=b"", status_code=HTTP_418_IM_A_TEAPOT)

    app = Litestar(
        route_handlers=[missing],
        exception_handlers={LitestarHTTPException: mine},
    )
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with LitestarTestClient(app=app, raise_server_exceptions=False) as client:
        response = client.get("/missing")

    # Assert
    assert response.status_code == HTTP_418_IM_A_TEAPOT


def test_litestar_field_errors_come_from_a_mapping_too() -> None:
    """Litestar reports them as a list or a mapping. Both normalise."""

    # Arrange
    class MappingError(Exception):
        extra: ClassVar[dict[str, str]] = {"amount": "must be an integer"}

    class ListedError(Exception):
        extra: ClassVar[list[Any]] = [
            {"key": "amount", "message": "must be an integer"},
            "bare",
        ]

    class NothingError(Exception):
        extra = None

    # Act & Assert
    assert _field_errors(MappingError()) == [
        {"loc": ["amount"], "msg": "must be an integer"}
    ]
    assert _field_errors(ListedError()) == [
        {"loc": ["amount"], "msg": "must be an integer"},
        {"loc": [], "msg": "bare"},
    ]
    assert _field_errors(NothingError()) == []


def test_the_tmf_message_folds_in_the_field_errors() -> None:
    """TMF has no list member, so `message` carries what the client must fix."""
    # Arrange
    app = _fastapi()
    Grelmicro(uses=[ErrorResponses.tmf()]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/charge", json={"amount": "abc"})

    # Assert
    message = response.json()["message"]
    assert "body.amount" in message
    assert "valid integer" in message


# --- What the review found ----------------------------------------------


def test_a_status_outside_the_registry_still_renders() -> None:
    """A service may answer with a code IANA never registered, and a proxy will.

    Looking one up must not fail while rendering a failure.
    """
    # Arrange
    app = FastAPI()

    @app.get("/odd")
    async def odd() -> dict[str, str]:
        raise FastAPIHTTPException(UNREGISTERED_STATUS, "nope")

    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/odd")

    # Assert
    assert response.status_code == UNREGISTERED_STATUS
    assert response.json()["title"] == "Error"
    assert response.json()["detail"] == "nope"


@pytest.mark.parametrize("status", [204, 304])
def test_a_bodiless_status_stays_bodiless(status: int) -> None:
    """The protocol says no body, whatever format the app answers in."""
    # Arrange
    app = FastAPI()

    @app.get("/empty")
    async def empty() -> dict[str, str]:
        raise FastAPIHTTPException(status)

    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/empty")

    # Assert
    assert response.status_code == status
    assert response.content == b""


def test_a_mapping_detail_becomes_extension_members() -> None:
    """FastAPI documents a dict there, and it is what a member already is."""
    # Arrange
    app = FastAPI()

    @app.get("/funds")
    async def funds() -> dict[str, str]:
        raise FastAPIHTTPException(
            HTTP_409_CONFLICT,
            detail={"code": "insufficient_funds", "balance": BALANCE},
        )

    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        body = client.get("/funds").json()

    # Assert
    assert body["code"] == "insufficient_funds"
    assert body["balance"] == BALANCE
    assert "detail" not in body


def test_a_list_detail_becomes_the_errors_member() -> None:
    """`errors` is the name an RFC 9457 reader expects a list under."""
    # Arrange
    app = FastAPI()

    @app.get("/many")
    async def many() -> dict[str, str]:
        raise FastAPIHTTPException(
            HTTP_400_BAD_REQUEST, detail=[{"field": "amount"}]
        )

    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        body = client.get("/many").json()

    # Assert
    assert body["errors"] == [{"field": "amount"}]


def test_an_extension_cannot_displace_a_standard_member() -> None:
    """The five names are RFC 9457's, and a body that lies is worse."""
    # Arrange
    app = FastAPI()

    @app.get("/sneaky")
    async def sneaky() -> dict[str, str]:
        raise FastAPIHTTPException(
            HTTP_409_CONFLICT, detail={"status": 200, "balance": BALANCE}
        )

    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        body = client.get("/sneaky").json()

    # Assert
    assert body["status"] == HTTP_409_CONFLICT
    assert body["balance"] == BALANCE


def test_a_wait_that_is_not_a_number_carries_no_header_in_any_format() -> None:
    """A refusal must not fail while refusing, whichever format renders it."""
    # Act
    for component in (ErrorResponses(), ErrorResponses.tmf()):
        rendered = component.render_status(
            HTTP_409_CONFLICT, extensions={"retry_after": "soon"}
        )

        # Assert
        assert "retry-after" not in rendered.headers


@pytest.mark.parametrize("status", [204, 304])
def test_litestar_bodiless_status_stays_bodiless(status: int) -> None:
    """The protocol rule holds on Litestar too."""

    # Arrange
    @get("/empty")
    async def empty() -> dict[str, str]:
        raise LitestarHTTPException(status_code=status)

    app = Litestar(route_handlers=[empty])
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with LitestarTestClient(app=app, raise_server_exceptions=False) as client:
        response = client.get("/empty")

    # Assert
    assert response.status_code == status
    assert response.content == b""


def test_no_body_schema_is_published_when_both_names_are_taken() -> None:
    """An empty `$ref` is not valid OpenAPI, so FastAPI's entry is kept."""

    # Arrange
    class ProblemDetail(BaseModel):
        """The app's own, under the plain name."""

        mine: str

    class GrelmicroProblemDetail(BaseModel):
        """The app's own, under the qualified name too."""

        also_mine: str

    app = FastAPI()

    @app.post(
        "/charge",
        responses={
            402: {"model": ProblemDetail},
            403: {"model": GrelmicroProblemDetail},
        },
    )
    async def charge(body: Charge) -> dict[str, bool]:  # noqa: ARG001
        return {"ok": True}

    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    content = app.openapi()["paths"]["/charge"]["post"]["responses"]["422"][
        "content"
    ]

    # Assert
    assert content == {
        "application/json": {
            "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
        }
    }


def test_documenting_before_install_still_publishes_the_right_format() -> None:
    """The order of the two calls must not decide what the schema claims."""
    # Arrange
    memory = MemoryProvider()
    app = FastAPI()

    @app.post("/charge")
    async def charge() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(IdempotencyMiddleware, idempotency=Idempotency("http"))
    document_idempotency(app)
    Grelmicro(uses=[memory, Cache(memory), ErrorResponses.tmf()]).install(app)

    # Act
    content = app.openapi()["paths"]["/charge"]["post"]["responses"]["409"][
        "content"
    ]

    # Assert
    assert content == {
        "application/json": {
            "schema": {"$ref": "#/components/schemas/TMFError"}
        }
    }


def _dangling(schema: dict[str, Any]) -> list[str]:
    """Return every component name pointed at but not defined."""
    defined = set(schema.get("components", {}).get("schemas", {}))

    def walk(node: object) -> set[str]:
        if isinstance(node, dict):
            found: set[str] = set()
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    found.add(value.rsplit("/", 1)[-1])
                else:
                    found |= walk(value)
            return found
        if isinstance(node, list):
            found = set()
            for item in node:
                found |= walk(item)
            return found
        return set()

    return sorted(walk(schema) - defined)


def test_the_replaced_validation_models_are_dropped() -> None:
    """A model no response uses reads as though some operation still does."""
    # Arrange
    app = _fastapi()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    schema = app.openapi()

    # Assert
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    assert "ValidationError" not in schema["components"]["schemas"]
    assert _dangling(schema) == []


def test_a_kept_validation_model_keeps_what_it_points_at() -> None:
    """`HTTPValidationError` holds a list of `ValidationError`.

    Dropping the second while the first survives would leave a `$ref` to
    nothing, which is the same defect as publishing an empty one.
    """

    # Arrange
    class ProblemDetail(BaseModel):
        """The app's own, under the plain name."""

        mine: str

    class GrelmicroProblemDetail(BaseModel):
        """The app's own, under the qualified name too."""

        also_mine: str

    app = FastAPI()

    @app.post(
        "/charge",
        responses={
            402: {"model": ProblemDetail},
            403: {"model": GrelmicroProblemDetail},
        },
    )
    async def charge(body: Charge) -> dict[str, bool]:  # noqa: ARG001
        return {"ok": True}

    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    schema = app.openapi()

    # Assert
    assert "HTTPValidationError" in schema["components"]["schemas"]
    assert "ValidationError" in schema["components"]["schemas"]
    assert _dangling(schema) == []


def test_an_app_without_the_component_keeps_its_schema() -> None:
    """Nothing is rewritten, so nothing is pruned."""
    # Arrange
    app = _fastapi()
    Grelmicro(uses=[]).install(app)

    # Act
    schemas = app.openapi()["components"]["schemas"]

    # Assert
    assert "HTTPValidationError" in schemas
    assert "ValidationError" in schemas


def test_keeping_your_validation_handler_keeps_your_schema() -> None:
    """A shape the app still answers with must stay in the schema.

    Rewriting the generated `422` for an app that kept its own handler
    would publish a body it does not send.
    """
    # Arrange
    app = _fastapi()

    async def mine(request: Request, exc: Exception) -> Response:  # noqa: ARG001
        return JSONResponse({"my_errors": []}, status_code=422)

    app.add_exception_handler(RequestValidationError, mine)
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        body = client.post("/charge", json={"amount": "abc"}).json()
    schema = app.openapi()

    # Assert
    assert body == {"my_errors": []}
    assert schema["paths"]["/charge"]["post"]["responses"]["422"][
        "content"
    ] == {
        "application/json": {
            "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
        }
    }
    assert "ValidationError" in schema["components"]["schemas"]


def test_a_header_the_app_set_wins_whatever_case_it_used() -> None:
    """Header names are case insensitive, so a merge must be too.

    Keeping both `Cache-Control` and `cache-control` emits two
    contradictory directives instead of one answer.
    """
    # Arrange
    app = FastAPI()

    @app.get("/auth")
    async def auth() -> dict[str, str]:
        raise FastAPIHTTPException(
            HTTP_401_UNAUTHORIZED,
            "No",
            headers={
                "WWW-Authenticate": "Bearer",
                "Cache-Control": "no-cache",
            },
        )

    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/auth")

    # Assert
    assert response.headers["www-authenticate"] == "Bearer"
    # One value, not two contradictory ones.
    assert "," not in response.headers["cache-control"]


def test_tmf_keeps_a_mapping_detail() -> None:
    """TMF has no extension member, so `message` carries it or nothing does."""
    # Arrange
    app = FastAPI()

    @app.get("/x")
    async def boom() -> dict[str, str]:
        raise FastAPIHTTPException(
            HTTP_400_BAD_REQUEST, detail={"code": "X", "field": "y"}
        )

    Grelmicro(uses=[ErrorResponses.tmf()]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        message = client.get("/x").json()["message"]

    # Assert
    assert "code: X" in message
    assert "field: y" in message


def test_litestar_schema_follows_the_registered_format() -> None:
    """The promise that the schema describes the wire holds on Litestar too."""

    # Arrange
    @post("/charge")
    async def charge(data: Charge) -> dict[str, bool]:  # noqa: ARG001
        return {"ok": True}

    app = Litestar(route_handlers=[charge])
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with LitestarTestClient(app=app) as client:
        schema = client.get("/schema/openapi.json").json()

    # Assert
    assert schema["paths"]["/charge"]["post"]["responses"]["400"][
        "content"
    ] == {
        PROBLEM_MEDIA_TYPE: {
            "schema": {"$ref": "#/components/schemas/ProblemDetail"}
        }
    }
    assert "ProblemDetail" in schema["components"]["schemas"]


def test_litestar_schema_follows_the_tmf_format() -> None:
    """A TMF service publishes `TMFError`, not `ProblemDetail`."""

    # Arrange
    @post("/charge")
    async def charge(data: Charge) -> dict[str, bool]:  # noqa: ARG001
        return {"ok": True}

    app = Litestar(route_handlers=[charge])
    Grelmicro(uses=[ErrorResponses.tmf()]).install(app)

    # Act
    with LitestarTestClient(app=app) as client:
        schema = client.get("/schema/openapi.json").json()

    # Assert
    assert schema["paths"]["/charge"]["post"]["responses"]["400"][
        "content"
    ] == {
        "application/json": {
            "schema": {"$ref": "#/components/schemas/TMFError"}
        }
    }


def test_litestar_schema_is_untouched_without_the_component() -> None:
    """Nothing is rewritten, so Litestar's own shape stays."""

    # Arrange
    @post("/charge")
    async def charge(data: Charge) -> dict[str, bool]:  # noqa: ARG001
        return {"ok": True}

    app = Litestar(route_handlers=[charge])
    Grelmicro(uses=[]).install(app)

    # Act
    with LitestarTestClient(app=app) as client:
        schema = client.get("/schema/openapi.json").json()

    # Assert
    generated = schema["paths"]["/charge"]["post"]["responses"]["400"][
        "content"
    ]["application/json"]["schema"]
    assert set(generated["required"]) == {"detail", "status_code"}


def test_litestar_leaves_a_response_the_app_declared() -> None:
    """Only what Litestar generated is rewritten. Yours is yours."""

    # Arrange
    class Mine(BaseModel):
        """A body the app documents itself."""

        mine: str

    @get("/own", responses={409: ResponseSpec(data_container=Mine)})
    async def own() -> dict[str, bool]:
        return {"ok": True}

    app = Litestar(route_handlers=[own])
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with LitestarTestClient(app=app) as client:
        schema = client.get("/schema/openapi.json").json()

    # Assert
    content = schema["paths"]["/own"]["get"]["responses"]["409"]["content"]
    assert "application/json" in content
    assert PROBLEM_MEDIA_TYPE not in content


def test_litestar_without_a_schema_still_starts() -> None:
    """`openapi_config=None` publishes nothing, so there is nothing to rewrite.

    Asking the plugin to build a schema there raises rather than returning
    nothing, which would take the app down in its own lifespan.
    """

    # Arrange
    @get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    app = Litestar(route_handlers=[ping], openapi_config=None)
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with LitestarTestClient(app=app) as client:
        response = client.get("/ping")

    # Assert
    assert response.status_code == HTTP_200_OK


def test_litestar_does_not_replace_a_model_of_yours() -> None:
    """An app may already publish a `ProblemDetail` that is not ours."""

    # Arrange
    @post("/items")
    async def items(data: ProblemDetail) -> ProblemDetail:
        return data

    app = Litestar(route_handlers=[items])
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with LitestarTestClient(app=app) as client:
        schemas = client.get("/schema/openapi.json").json()["components"][
            "schemas"
        ]

    # Assert
    assert sorted(schemas["ProblemDetail"]["properties"]) == ["mine"]
    assert "GrelmicroProblemDetail" in schemas


def test_litestar_keeping_your_handler_keeps_your_schema() -> None:
    """The schema must not describe a body the app does not send."""

    # Arrange
    @post("/charge")
    async def charge(data: Charge) -> dict[str, bool]:  # noqa: ARG001
        return {"ok": True}

    def mine(
        request: LitestarRequest,  # noqa: ARG001
        exc: Exception,  # noqa: ARG001
    ) -> LitestarResponse:
        return LitestarResponse(
            content=b'{"mine":true}',
            status_code=HTTP_400_BAD_REQUEST,
            media_type="application/json",
        )

    app = Litestar(
        route_handlers=[charge],
        exception_handlers={LitestarHTTPException: mine},
    )
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with LitestarTestClient(app=app, raise_server_exceptions=False) as client:
        body = client.post("/charge", json={"amount": "abc"}).content
        schema = client.get("/schema/openapi.json").json()

    # Assert
    assert body == b'{"mine":true}'
    generated = schema["paths"]["/charge"]["post"]["responses"][
        str(HTTP_400_BAD_REQUEST)
    ]["content"]["application/json"]["schema"]
    assert set(generated["required"]) == {"detail", "status_code"}


def test_a_validation_message_the_framework_had_survives() -> None:
    """It is the one actionable string, and both formats must keep it."""

    # Arrange
    @get("/v")
    async def bad() -> dict[str, str]:
        raise ValidationException(VALIDATION_MESSAGE)

    for errors in (ErrorResponses(), ErrorResponses.tmf()):
        app = Litestar(route_handlers=[bad])
        Grelmicro(uses=[errors]).install(app)

        # Act
        with LitestarTestClient(
            app=app, raise_server_exceptions=False
        ) as client:
            body = client.get("/v").json()

        # Assert
        said = body.get("detail") or body.get("message")
        assert said == VALIDATION_MESSAGE
        # An empty field-error list has nothing to say, in either format.
        assert "errors" not in body
        assert "[]" not in str(said)


def test_litestar_says_nothing_when_both_names_are_taken() -> None:
    """Litestar's own entry beats one pointing at nothing."""

    # Arrange
    @post("/items")
    async def items(data: ProblemDetail) -> GrelmicroProblemDetail:
        return GrelmicroProblemDetail(also_mine=data.mine)

    app = Litestar(route_handlers=[items])
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with LitestarTestClient(app=app) as client:
        schema = client.get("/schema/openapi.json").json()

    # Assert
    generated = schema["paths"]["/items"]["post"]["responses"]["400"][
        "content"
    ]["application/json"]["schema"]
    assert set(generated["required"]) == {"detail", "status_code"}
    assert sorted(schema["components"]["schemas"]["ProblemDetail"]) != []


@pytest.mark.parametrize(
    ("build", "raise_401"),
    [("fastapi", True), ("litestar", True)],
)
def test_the_safety_headers_cannot_be_overridden(
    build: str,
    raise_401: bool,  # noqa: ARG001, FBT001
) -> None:
    """Grelmicro adds them because of the body it renders, so it owns them.

    `no-store` because a refusal is about one client at one moment, and
    `nosniff` because that body reflects the request path back. A cacheable
    `401` sitting in a shared cache is somebody else's session problem.
    """
    # Arrange
    hostile = {
        "Cache-Control": "public, max-age=3600",
        "X-Content-Type-Options": "",
        "WWW-Authenticate": "Bearer",
    }
    if build == "fastapi":
        app: Any = FastAPI()

        @app.get("/auth")
        async def auth() -> dict[str, str]:
            raise FastAPIHTTPException(
                HTTP_401_UNAUTHORIZED, "no", headers=hostile
            )

        Grelmicro(uses=[ErrorResponses()]).install(app)
        with TestClient(app, raise_server_exceptions=False) as client:
            response: Any = client.get("/auth")
    else:

        @get("/auth")
        async def litestar_auth() -> dict[str, str]:
            raise LitestarHTTPException(
                status_code=HTTP_401_UNAUTHORIZED,
                detail="no",
                headers=hostile,
            )

        app = Litestar(route_handlers=[litestar_auth])
        Grelmicro(uses=[ErrorResponses()]).install(app)
        with LitestarTestClient(
            app=app, raise_server_exceptions=False
        ) as client:
            response = client.get("/auth")

    # Assert
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    # What the app said that is genuinely its own still wins.
    assert response.headers["www-authenticate"] == "Bearer"


def test_registering_your_handler_after_install_keeps_your_schema() -> None:
    """The order of the two calls must not decide what the schema claims."""
    # Arrange
    app = _fastapi()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    async def mine(request: Request, exc: Exception) -> Response:  # noqa: ARG001
        return JSONResponse({"my_errors": []}, status_code=422)

    app.add_exception_handler(RequestValidationError, mine)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        body = client.post("/charge", json={"amount": "abc"}).json()
    schema = app.openapi()

    # Assert
    assert body == {"my_errors": []}
    assert schema["paths"]["/charge"]["post"]["responses"]["422"][
        "content"
    ] == {
        "application/json": {
            "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
        }
    }
    assert "ValidationError" in schema["components"]["schemas"]


def test_declaring_our_model_yourself_publishes_it_once() -> None:
    """The documented `responses={...}` pattern must not duplicate the body.

    FastAPI renders the same class with an explicit `default: None` where
    `model_json_schema` writes nothing, so comparing whole renderings
    published it twice under two names.
    """
    # Arrange
    app = FastAPI()

    @app.get("/quote", responses={429: {"model": GrelmicroProblem}})
    async def quote() -> dict[str, int]:
        return {"price": 42}

    @app.post("/charge")
    async def charge(body: Charge) -> dict[str, bool]:  # noqa: ARG001
        return {"ok": True}

    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    schemas = app.openapi()["components"]["schemas"]

    # Assert
    assert "GrelmicroProblemDetail" not in schemas
    assert sorted(schemas) == ["Charge", "ProblemDetail"]


def test_a_delay_that_is_not_a_real_number_carries_no_header() -> None:
    """Infinity and NaN pass every type check, and `ceil` raises on both."""
    # Act & Assert
    for wait in (float("inf"), float("-inf"), float("nan")):
        rendered = ErrorResponses().render_status(
            HTTP_409_CONFLICT, extensions={"retry_after": wait}
        )
        assert "retry-after" not in rendered.headers


def test_a_webhook_keeps_a_resolvable_reference() -> None:
    """FastAPI publishes webhooks beside paths, and both must stay valid.

    A webhook left out of the walk keeps a `$ref` to a component the walk
    then deletes, and an unresolvable ref fails every client generator.
    """
    # Arrange
    app = FastAPI()

    @app.webhooks.post("new-subscription")
    async def subscribed(body: Charge) -> None: ...

    @app.post("/charge")
    async def charge(body: Charge) -> dict[str, bool]:  # noqa: ARG001
        return {"ok": True}

    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    schema = app.openapi()

    # Assert
    assert _dangling(schema) == []
    assert schema["webhooks"]["new-subscription"]["post"]["responses"]["422"][
        "content"
    ] == {
        PROBLEM_MEDIA_TYPE: {
            "schema": {"$ref": "#/components/schemas/ProblemDetail"}
        }
    }


def test_no_model_is_published_when_nothing_validates() -> None:
    """A component nothing points at reads as though something answers with it."""
    # Arrange
    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    schemas = app.openapi().get("components", {}).get("schemas", {})

    # Assert
    assert "ProblemDetail" not in schemas


def test_a_handler_named_like_ours_is_still_yours() -> None:
    """`validation_error` is the obvious name, so identity decides, not it."""
    # Arrange
    app = _fastapi()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    async def validation_error(request: Request, exc: Exception) -> Response:  # noqa: ARG001
        return JSONResponse({"mine": True}, status_code=422)

    app.add_exception_handler(RequestValidationError, validation_error)

    # Act
    content = app.openapi()["paths"]["/charge"]["post"]["responses"]["422"][
        "content"
    ]

    # Assert
    assert content == {
        "application/json": {
            "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
        }
    }


def test_a_schema_built_before_your_handler_is_rebuilt() -> None:
    """The rewrite mutates FastAPI's cached dict, so the cache must go."""
    # Arrange
    app = _fastapi()
    Grelmicro(uses=[ErrorResponses()]).install(app)
    built = app.openapi()
    assert (
        PROBLEM_MEDIA_TYPE
        in built["paths"]["/charge"]["post"]["responses"]["422"]["content"]
    )

    async def mine(request: Request, exc: Exception) -> Response:  # noqa: ARG001
        return JSONResponse({"mine": True}, status_code=422)

    app.add_exception_handler(RequestValidationError, mine)

    # Act
    content = app.openapi()["paths"]["/charge"]["post"]["responses"]["422"][
        "content"
    ]

    # Assert
    assert content == {
        "application/json": {
            "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
        }
    }


def test_a_model_the_app_still_points_at_is_kept() -> None:
    """Only what nothing references is dropped."""
    # Arrange
    app = FastAPI()

    @app.post(
        "/charge",
        responses={
            409: {
                "content": {
                    "application/json": {
                        "schema": {
                            "$ref": "#/components/schemas/HTTPValidationError"
                        }
                    }
                }
            }
        },
    )
    async def charge(body: Charge) -> dict[str, bool]:  # noqa: ARG001
        return {"ok": True}

    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    schema = app.openapi()

    # Assert
    assert "HTTPValidationError" in schema["components"]["schemas"]
    assert _dangling(schema) == []
