"""Test on-demand jobs behind an HTTP app."""

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.status import (
    HTTP_202_ACCEPTED,
    HTTP_404_NOT_FOUND,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from grelmicro import Grelmicro
from grelmicro.http import ERROR_DOCS_BASE, ErrorResponses
from grelmicro.task import JobInfo, Progress, Tasks

pytestmark = [pytest.mark.timeout(10)]


async def _run_forever(_progress: Progress) -> None:
    """Run until cancelled."""
    await asyncio.Event().wait()


def test_job_submit_answers_503_when_the_queue_is_full() -> None:
    """A submit the job refuses leaves the endpoint as `503 request-refused`."""
    # Arrange
    tasks = Tasks(shutdown_timeout=0)
    job = tasks.job(max_concurrent=1, max_queued=0)(_run_forever)
    app = FastAPI()

    @app.post("/jobs", status_code=HTTP_202_ACCEPTED)
    async def start() -> JobInfo:
        return await job.submit()

    Grelmicro(uses=[tasks, ErrorResponses()]).install(app)

    with TestClient(app, raise_server_exceptions=False) as client:
        # Act
        first = client.post("/jobs")
        refused = client.post("/jobs")

    # Assert
    assert first.status_code == HTTP_202_ACCEPTED
    assert first.json()["status"] in {"queued", "running"}
    assert refused.status_code == HTTP_503_SERVICE_UNAVAILABLE
    assert refused.json()["type"] == f"{ERROR_DOCS_BASE}#request-refused"


def _load_job_snippet() -> ModuleType:
    """Import `docs/snippets/task/job.py` as a fresh module."""
    path = Path(__file__).resolve().parents[2] / "docs/snippets/task/job.py"
    spec = importlib.util.spec_from_file_location("snippet_task_job", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_job_documented_import_flow_works() -> None:
    """The docs example starts an import, polls it, and scopes it per user."""
    # Arrange
    snippet = _load_job_snippet()
    alice, bob = {"user-id": "alice"}, {"user-id": "bob"}

    with TestClient(snippet.app) as client:
        # Act
        started = client.post("/imports?upload_id=u-1", headers=alice)
        job_id = started.json()["id"]
        polled = client.get(f"/imports/{job_id}", headers=alice).json()
        while polled["finished_at"] is None:
            polled = client.get(f"/imports/{job_id}", headers=alice).json()
        as_bob = client.get(f"/imports/{job_id}", headers=bob)
        cancel_as_bob = client.post(f"/imports/{job_id}/cancel", headers=bob)
        cancel_finished = client.post(
            f"/imports/{job_id}/cancel", headers=alice
        )

    # Assert
    assert started.status_code == HTTP_202_ACCEPTED
    assert polled["status"] == "succeeded"
    assert polled["results"] == ["row-1", "row-2", "row-3"]
    assert (polled["done"], polled["total"]) == (3, 3)
    assert as_bob.status_code == HTTP_404_NOT_FOUND
    assert cancel_as_bob.status_code == HTTP_404_NOT_FOUND
    assert cancel_finished.json()["status"] == "succeeded"
