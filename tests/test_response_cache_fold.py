"""How far `CachedResponses` folds concurrent misses: `lock=`, like `@cached`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.errors import SettingsValidationError
from grelmicro.http import CachedResponses
from grelmicro.integrations.fastapi import CachedResponse
from tests._logs import records_of

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

    from grelmicro.types import BackendScope

pytestmark = [pytest.mark.timeout(10)]


def _app(component: CachedResponses, *uses: Coordination) -> FastAPI:
    """Return an app whose `/reads` route is cached."""
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), *uses, component])
    app = FastAPI()
    micro.install(app)

    @app.get("/reads", dependencies=[CachedResponse(ttl=60)])
    async def reads() -> dict[str, int]:
        return {"answer": 42}

    return app


def test_cached_responses_default_folds_in_the_process(
    mocker: MockerFixture,
) -> None:
    """The default never asks the lock backend, even when the app has one."""
    # Arrange
    backend = MemoryLockAdapter()
    acquire = mocker.spy(backend, "acquire")
    app = _app(
        CachedResponses(), Coordination(lock=backend, requires="process")
    )

    # Act
    with TestClient(app) as client:
        response = client.get("/reads")

    # Assert
    assert response.status_code == 200  # noqa: PLR2004
    acquire.assert_not_called()


def test_cached_responses_cluster_folds_through_the_lock_backend(
    mocker: MockerFixture,
) -> None:
    """`lock="cluster"` takes the app's lock on a miss."""
    # Arrange
    backend = MemoryLockAdapter()
    acquire = mocker.spy(backend, "acquire")
    app = _app(
        CachedResponses(lock="cluster"),
        Coordination(lock=backend, requires="process"),
    )

    # Act
    with TestClient(app) as client:
        response = client.get("/reads")

    # Assert
    assert response.status_code == 200  # noqa: PLR2004
    acquire.assert_called_once()


def test_cached_responses_cluster_without_lock_backend_reports_and_answers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A scope past the process with no lock backend is reported, and the read answered."""
    # Arrange
    app = _app(CachedResponses(lock="cluster"))

    # Act
    with TestClient(app) as client:
        response = client.get("/reads")

    # Assert
    assert response.status_code == 200  # noqa: PLR2004
    assert any(
        "could not fold" in record.getMessage()
        for record in records_of(caplog, "grelmicro")
    )


def test_cached_responses_none_never_folds(mocker: MockerFixture) -> None:
    """`lock=None` turns folding off, and the response is still cached."""
    # Arrange
    backend = MemoryLockAdapter()
    acquire = mocker.spy(backend, "acquire")
    app = _app(
        CachedResponses(lock=None),
        Coordination(lock=backend, requires="process"),
    )

    # Act
    with TestClient(app) as client:
        first = client.get("/reads")
        second = client.get("/reads")

    # Assert
    assert first.json() == second.json()
    acquire.assert_not_called()


@pytest.mark.parametrize("value", [True, "local"])
def test_cached_responses_lock_not_a_scope_is_refused(value: object) -> None:
    """Only a backend scope or `None` is a fold."""
    # Act / Assert
    with pytest.raises(SettingsValidationError, match="lock="):
        CachedResponses(lock=value)  # type: ignore[arg-type] # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("scope", ["process", "host", "cluster", None])
def test_cached_responses_from_config_keeps_the_scope(
    scope: BackendScope | None,
) -> None:
    """`from_config` takes the same `lock=`."""
    # Arrange
    from grelmicro.http import CachedResponsesConfig  # noqa: PLC0415

    # Act
    component = CachedResponses.from_config(CachedResponsesConfig(), lock=scope)

    # Assert
    assert component.asgi_middleware()[1]["lock"] == scope
