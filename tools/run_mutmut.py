"""Run mutmut with string-literal mutations disabled.

mutmut has no config switch to skip string mutations. In this codebase those
are almost all log and exception message text, so mutating them yields
survivors that would only die if tests asserted exact message strings, which
the project deliberately avoids. We drop the string operator from mutmut's
registry before generation. mutmut forks its worker pool, so the in-place edit
is inherited by every child.

Usage (via `just mutation`): uv run python tools/run_mutmut.py run
"""

import sys
from pathlib import Path

from mutmut.__main__ import cli
from mutmut.mutation import mutators

# Match `python -m mutmut`: put the project root first so the mutants/ copy
# shadows the editable install during the test runs.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Drop the string operator in place. file_mutation imported the same list
# object by reference, so this edit reaches generation. The registry is typed
# as an immutable Sequence but is a list at runtime.
mutators.mutation_operators[:] = [  # ty: ignore[invalid-assignment]
    (node_type, operator)
    for node_type, operator in mutators.mutation_operators
    if operator is not mutators.operator_string
]

if __name__ == "__main__":
    cli()
