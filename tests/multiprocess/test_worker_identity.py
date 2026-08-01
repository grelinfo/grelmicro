"""The worker identity must differ in every process that presents a token.

`gunicorn --preload` builds the application once and forks, so a worker
identity generated in the parent is inherited byte for byte by every
child. Two workers would then present the same lock token, and one could
release or extend a lease the other holds. Leader election is worse: its
token is the worker identity itself, so every child would read the record
holder as itself and believe it leads.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest

from grelmicro.coordination import _tokens
from grelmicro.coordination._tokens import (
    generate_task_token,
    generate_worker_id,
    resolve_worker,
)

from .harness import run_workers

if TYPE_CHECKING:
    from multiprocessing.synchronize import Barrier

    from .harness import Results

pytestmark = [
    pytest.mark.timeout(60),
    pytest.mark.skipif(sys.platform == "win32", reason="fork is POSIX only"),
    # `os.fork` in a process that already has threads is deprecated, and the
    # suite turns warnings into errors. Forking is the behaviour under test.
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

WORKERS = 4

PRELOADED_WORKER = generate_worker_id()
"""A worker identity minted at import time, as a preloaded app would."""


def report_identity(barrier: Barrier, results: Results, worker: str) -> None:
    """Report the identity this process resolves for a shared worker id."""
    barrier.wait()
    results.append(resolve_worker(worker))


def test_forked_workers_resolve_distinct_identities() -> None:
    """Every child of a pre-fork server presents its own identity."""
    # Act
    identities = run_workers(
        report_identity,
        WORKERS,
        PRELOADED_WORKER,
        start_method="fork",
    )

    # Assert
    assert len(identities) == WORKERS
    assert len(set(identities)) == WORKERS
    assert resolve_worker(PRELOADED_WORKER) not in identities
    assert all(identity.startswith(PRELOADED_WORKER) for identity in identities)


def test_spawned_workers_resolve_distinct_identities() -> None:
    """A spawned worker mints its own identity, so nothing is inherited."""
    # Act
    identities = run_workers(
        mint_and_report_identity, WORKERS, start_method="spawn"
    )

    # Assert
    assert len(set(identities)) == WORKERS


def mint_and_report_identity(barrier: Barrier, results: Results) -> None:
    """Mint an identity the way an independently imported app would."""
    worker = generate_worker_id()
    barrier.wait()
    results.append(resolve_worker(worker))


def test_unforked_process_leaves_the_identity_untouched() -> None:
    """A deployment that never forks sees the identity it generated."""
    worker = generate_worker_id()

    assert resolve_worker(worker) == worker


async def test_task_token_carries_the_resolved_identity() -> None:
    """Lock tokens are built from the resolved identity, not the raw one."""
    worker = generate_worker_id()

    token = generate_task_token(worker)

    assert token.startswith(f"{resolve_worker(worker)}:task:")


def test_a_changed_pid_diverges_the_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a child runs this branch, so drive it in process for coverage."""
    # Arrange
    monkeypatch.setattr(_tokens, "_origin_pid", os.getpid() + 1)
    monkeypatch.setattr(_tokens, "_fork_suffixes", {})
    worker = generate_worker_id()

    # Act
    diverged = resolve_worker(worker)

    # Assert
    assert diverged != worker
    assert diverged.startswith(f"{worker}.")


def test_the_suffix_is_stable_within_one_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second call reuses the suffix, so a token still matches its check.

    Two suffixes in one process would leave a holder unable to release its
    own lock, because the ownership check would build a different token.
    """
    # Arrange
    monkeypatch.setattr(_tokens, "_origin_pid", os.getpid() + 1)
    monkeypatch.setattr(_tokens, "_fork_suffixes", {})
    worker = generate_worker_id()

    # Act
    first = resolve_worker(worker)
    second = resolve_worker(worker)

    # Assert
    assert first == second
