"""GrelMicro Test Config."""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    """AnyIO Backend."""
    return "asyncio"
