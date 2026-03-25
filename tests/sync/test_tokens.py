"""Tests for Synchronization Tokens."""

from threading import get_ident
from uuid import uuid1

import pytest
from anyio import get_current_task

from grelmicro.sync._tokens import (
    generate_task_token,
    generate_thread_token,
    generate_token_nonce,
    generate_worker_id,
)

pytestmark = pytest.mark.anyio

WORKER_ID_LENGTH = 16


def test_generate_worker_id() -> None:
    """Test generate_worker_id returns an 8-character hex string."""
    # Act
    worker_id = generate_worker_id()

    # Assert
    assert isinstance(worker_id, str)
    assert len(worker_id) == WORKER_ID_LENGTH
    int(worker_id, 16)  # valid hex


def test_generate_worker_id_unique() -> None:
    """Test generate_worker_id returns unique values."""
    # Act
    id1 = generate_worker_id()
    id2 = generate_worker_id()

    # Assert
    assert id1 != id2


async def test_generate_task_token_with_uuid() -> None:
    """Test generate_task_token with a UUID worker."""
    # Arrange
    worker = uuid1()

    # Act
    token = generate_task_token(worker)

    # Assert
    task_id = get_current_task().id
    assert token == f"{worker}:task:{task_id}"


async def test_generate_task_token_with_string() -> None:
    """Test generate_task_token with a string worker name."""
    # Act
    token = generate_task_token("my-worker")

    # Assert
    task_id = get_current_task().id
    assert token == f"my-worker:task:{task_id}"


async def test_generate_task_token_with_nonce() -> None:
    """Test generate_task_token appends nonce to the token."""
    # Arrange
    nonce = ":42"

    # Act
    token = generate_task_token("my-worker", nonce)

    # Assert
    task_id = get_current_task().id
    assert token == f"my-worker:task:{task_id}:42"


async def test_generate_task_token_deterministic() -> None:
    """Test generate_task_token is deterministic within the same task."""
    # Act / Assert
    assert generate_task_token("w") == generate_task_token("w")


def test_generate_token_nonce() -> None:
    """Test generate_token_nonce returns a nonce suffix string."""
    # Act
    nonce = generate_token_nonce()

    # Assert
    assert isinstance(nonce, str)
    assert nonce.startswith(":")


def test_generate_token_nonce_unique() -> None:
    """Test generate_token_nonce returns unique values on each call."""
    # Act
    nonce1 = generate_token_nonce()
    nonce2 = generate_token_nonce()

    # Assert
    assert nonce1 != nonce2


def test_generate_thread_token_with_uuid() -> None:
    """Test generate_thread_token with a UUID worker."""
    # Arrange
    worker = uuid1()

    # Act
    token = generate_thread_token(worker)

    # Assert
    thread_id = get_ident()
    assert token == f"{worker}:thread:{thread_id}"


def test_generate_thread_token_with_string() -> None:
    """Test generate_thread_token with a string worker name."""
    # Act
    token = generate_thread_token("my-worker")

    # Assert
    thread_id = get_ident()
    assert token == f"my-worker:thread:{thread_id}"


def test_generate_thread_token_with_nonce() -> None:
    """Test generate_thread_token appends nonce to the token."""
    # Arrange
    nonce = ":42"

    # Act
    token = generate_thread_token("my-worker", nonce)

    # Assert
    thread_id = get_ident()
    assert token == f"my-worker:thread:{thread_id}:42"


def test_generate_thread_token_deterministic() -> None:
    """Test generate_thread_token is deterministic within the same thread."""
    # Act / Assert
    assert generate_thread_token("w") == generate_thread_token("w")
