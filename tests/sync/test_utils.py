"""Tests for Synchronization Utilities."""

from uuid import UUID, uuid1

import pytest

from grelmicro.sync._utils import (
    generate_task_token,
    generate_thread_token,
    generate_worker_id,
    generate_worker_namespace,
)

pytestmark = pytest.mark.anyio

UUID_V1 = 1
UUID_V3 = 3


def test_generate_worker_id() -> None:
    """Test generate_worker_id returns a UUIDv1."""
    # Act
    worker_id = generate_worker_id()

    # Assert
    assert isinstance(worker_id, UUID)
    assert worker_id.version == UUID_V1


def test_generate_worker_id_unique() -> None:
    """Test generate_worker_id returns unique values."""
    # Act
    id1 = generate_worker_id()
    id2 = generate_worker_id()

    # Assert
    assert id1 != id2


def test_generate_worker_namespace() -> None:
    """Test generate_worker_namespace returns a UUIDv3."""
    # Act
    ns = generate_worker_namespace("my-worker")

    # Assert
    assert isinstance(ns, UUID)
    assert ns.version == UUID_V3


def test_generate_worker_namespace_deterministic() -> None:
    """Test generate_worker_namespace is deterministic."""
    # Act / Assert
    assert generate_worker_namespace("a") == generate_worker_namespace("a")
    assert generate_worker_namespace("a") != generate_worker_namespace("b")


async def test_generate_task_token_with_uuid() -> None:
    """Test generate_task_token with a UUID worker namespace."""
    # Arrange
    worker = uuid1()

    # Act
    token = generate_task_token(worker)

    # Assert
    parsed = UUID(token)
    assert parsed.version == UUID_V3


async def test_generate_task_token_with_string() -> None:
    """Test generate_task_token with a string worker name."""
    # Act
    token = generate_task_token("my-worker")

    # Assert
    parsed = UUID(token)
    assert parsed.version == UUID_V3


async def test_generate_task_token_deterministic() -> None:
    """Test generate_task_token is deterministic within the same task."""
    # Act / Assert
    assert generate_task_token("w") == generate_task_token("w")


def test_generate_thread_token_with_uuid() -> None:
    """Test generate_thread_token with a UUID worker namespace."""
    # Arrange
    worker = uuid1()

    # Act
    token = generate_thread_token(worker)

    # Assert
    parsed = UUID(token)
    assert parsed.version == UUID_V3


def test_generate_thread_token_with_string() -> None:
    """Test generate_thread_token with a string worker name."""
    # Act
    token = generate_thread_token("my-worker")

    # Assert
    parsed = UUID(token)
    assert parsed.version == UUID_V3


def test_generate_thread_token_deterministic() -> None:
    """Test generate_thread_token is deterministic within the same thread."""
    # Act / Assert
    assert generate_thread_token("w") == generate_thread_token("w")
