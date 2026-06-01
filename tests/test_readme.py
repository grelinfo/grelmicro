"""Execute the Python examples in README.md to catch drift.

The README is the front door and its examples are the first code a new
user runs. `docs/index.md` is generated from it by the `readme-to-docs`
hook, so the examples live literally in README.md (GitHub does not
process snippet includes) rather than in `docs/snippets`. This test
gives them the same import-and-runtime coverage the snippets get: each
fenced ``python`` block is imported as a module, so a renamed symbol or
broken import fails the suite.

The blocks construct apps and register routes and tasks. None of them
serve or run an event loop at module level, so importing the block has
no side effects.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_README = Path(__file__).resolve().parent.parent / "README.md"
_BLOCK = re.compile(r"^```python\n(.*?)^```", re.MULTILINE | re.DOTALL)
_BLOCKS = _BLOCK.findall(_README.read_text(encoding="utf-8"))

# The README ships at least these front-door examples: one route, the
# lifespan variant, and the full FastAPI integration.
_MIN_EXAMPLES = 3


def test_readme_has_python_examples() -> None:
    """The README still carries the front-door examples."""
    assert len(_BLOCKS) >= _MIN_EXAMPLES


@pytest.mark.parametrize(
    "code", _BLOCKS, ids=[f"block{i}" for i in range(len(_BLOCKS))]
)
def test_readme_example_executes(code: str, tmp_path: Path) -> None:
    """Each README python block imports and constructs without error."""
    path = tmp_path / "readme_block.py"
    path.write_text(code, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("readme_block", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
