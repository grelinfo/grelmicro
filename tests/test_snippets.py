"""Execute documentation snippets to catch import and runtime drift.

`compileall` and the MkDocs build only check that snippets parse. This
module goes one step further and runs each snippet the way a reader
would, so import drift, renamed symbols, a route that answers `500`, and
an output block that no longer matches surface as test failures.

Snippets are tiered:

- `RUN`: executed with no special setup (the default).
- `ENV`: executed with the documented environment variables set.
- `SCRIPT`: also executed as `__main__`, so the `main()` a reader runs
  is covered too.
- `NEEDS_SERVICE`: imported, never executed as a script, because
  running it needs Redis, Postgres, a signal, or a long wait.
- `COMPILE_ONLY`: parsed but not executed, because running them at
  import time has global side effects (they call `asyncio.run(...)`).
  These are still covered by `compileall` and the MkDocs build.

The inline blocks on the pages are checked too. A block that only runs
inside a function says it is a `fragment`, a block that imports
anything imports every grelmicro name it uses, and a block presented as
a snippet's output has to match what that snippet prints.
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import importlib.util
import logging
import os
import pkgutil
import re
import runpy
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import grelmicro
from grelmicro._paths import walk_routes
from tests._containers import CONTAINER_LOG_TIMEOUT

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

_ROOT = Path(__file__).resolve().parent.parent
_DOCS_DIR = _ROOT / "docs"
_SNIPPETS_DIR = _DOCS_DIR / "snippets"

# `--8<-- "path/to/snippet.py"`, optionally with a `:start:end` line range.
_INCLUDE_RE = re.compile(r'--8<--\s*"([^":]+)(?::\d+)*"')

# A fenced block, with the indent an admonition or a tab adds to it.
_FENCE_RE = re.compile(
    r"^(?P<indent>[ \t]*)```(?P<info>[^\n`]*)\n(?P<body>.*?)^(?P=indent)```",
    re.MULTILINE | re.DOTALL,
)

_OUTSIDE_FUNCTION_RE = re.compile(
    r"outside (?:async |of an asynchronous )?function"
)
"""The `SyntaxError` a statement raises when it only runs inside a function."""

# Snippets whose module body runs an event loop with global side effects
# (logging / tracing setup). Compiled and built by MkDocs, not run here.
_COMPILE_ONLY = {
    "trace/component.py",
    "trace/autoinstrument.py",
    "log/component.py",
}

# Snippets a reader runs against something this suite does not have.
# They are imported like every other snippet, and their `main()` is not
# executed here.
_NEEDS_SERVICE = {
    "cache/batch.py": "Redis",
    "cache/get_or_set.py": "Redis",
    "cache/key.py": "Redis",
    "cache/redis_basic.py": "Redis",
    "cache/refresh.py": "Redis",
    "cache/stream.py": "Redis",
    "cache/tags.py": "Redis",
    "coordination/leaderelection_asyncio.py": "Redis, and it runs forever",
    "coordination/quickstart_lock.py": "Redis",
    "idempotency/run.py": "Redis",
    "log/dict_config.py": "a server it would keep running",
    "outbox/quickstart.py": "Postgres",
    "wiring/simple_fastapi_app.py": "Redis",
    "task/graceful_shutdown.py": "a signal, and it waits for one",
    "task/quickstart.py": "twelve seconds of schedule",
}

# Snippets that read required configuration from the environment. They
# are run with the documented variables set.
_ENV = {
    "resilience/fallback_environmental.py": {
        "GREL_FALLBACK_RECS_WHEN": "builtins.ValueError",
        "GREL_FALLBACK_RECS_DEFAULT": "[]",
    },
    "resilience/timeout_environmental.py": {
        "GREL_TIMEOUT_DB_SECONDS": "2.0",
    },
    "coordination/postgres_backend.py": {
        "POSTGRES_URL": "postgresql://user:password@localhost:5432/db",
    },
    "outbox/quickstart.py": {
        "POSTGRES_URL": "postgresql://user:password@localhost:5432/db",
    },
    "deployment/composition_root.py": {
        "REDIS_URL": "redis://localhost:6379/0",
    },
}


# The JWT snippet loads its verification key the way an application does,
# from the environment. A key is generated here so the snippet runs against
# real material rather than a placeholder that would never parse.
def _public_key_pem() -> str:
    generated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        generated.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


_ENV["security/jwt.py"] = {"JWT_PUBLIC_KEY": _public_key_pem()}

# The outbound token snippets read the client secret from the environment,
# the way a deployment hands it over.
_OAUTH_CLIENT_ENV = {
    "GREL_ENV_LOAD": "1",
    "GREL_OAUTHCLIENT_CLIENT_SECRET": "snippet-client-secret",
}
_ENV["security/tokens.py"] = _OAUTH_CLIENT_ENV
_ENV["security/tokens_exchange.py"] = _OAUTH_CLIENT_ENV

_ALL = sorted(
    p.relative_to(_SNIPPETS_DIR).as_posix() for p in _SNIPPETS_DIR.rglob("*.py")
)
_RUNNABLE = [rel for rel in _ALL if rel not in _COMPILE_ONLY]


def _source(rel: str) -> str:
    return (_SNIPPETS_DIR / rel).read_text()


def _defines_main(rel: str) -> bool:
    tree = ast.parse(_source(rel))
    return any(
        isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and node.name == "main"
        for node in tree.body
    )


def _runs_as_script(rel: str) -> bool:
    return '__name__ == "__main__"' in _source(rel)


_SCRIPT = [
    rel
    for rel in _RUNNABLE
    if _runs_as_script(rel) and rel not in _NEEDS_SERVICE
]


def _import_snippet(rel: str) -> ModuleType:
    path = _SNIPPETS_DIR / rel
    spec = importlib.util.spec_from_file_location(
        f"snippet_{rel.replace('/', '_').removesuffix('.py')}", path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _blocks(page: Path) -> Iterator[tuple[int, str, str]]:
    """Yield `(line, info string, dedented body)` for each fenced block."""
    text = page.read_text()
    for match in _FENCE_RE.finditer(text):
        yield (
            text[: match.start()].count("\n") + 1,
            match.group("info").strip(),
            textwrap.dedent(match.group("body")),
        )


def _pages() -> Iterator[Path]:
    return iter(sorted(_DOCS_DIR.rglob("*.md")))


def _grelmicro_public_names() -> frozenset[str]:
    """Every name grelmicro exports, from every public module."""
    names: set[str] = set(getattr(grelmicro, "__all__", ()))
    for found in pkgutil.walk_packages(grelmicro.__path__, "grelmicro."):
        if any(part.startswith("_") for part in found.name.split(".")):
            continue
        with contextlib.suppress(Exception):
            module = importlib.import_module(found.name)
            names.update(getattr(module, "__all__", ()))
    return frozenset(names)


_PUBLIC_NAMES = _grelmicro_public_names()


def test_snippets_present() -> None:
    """The snippet tiers reference files that still exist."""
    for rel in _COMPILE_ONLY | set(_ENV) | set(_NEEDS_SERVICE):
        assert (_SNIPPETS_DIR / rel).is_file(), rel


def test_no_orphan_snippets() -> None:
    """Every snippet is included by a page.

    `check_paths` fails an include pointing at a missing file. Nothing
    catches the reverse, so a snippet no page renders still costs test
    time and reads as a live example to anyone browsing the tree.
    """
    included = {
        match
        for page in _pages()
        for match in _INCLUDE_RE.findall(page.read_text())
    }
    snippets = {rel for rel in _ALL if not rel.endswith("__init__.py")}
    orphans = sorted(snippets - included)
    assert not orphans, (
        f"snippets included by no page: {orphans}. Include them or delete them."
    )


def _importable(name: str) -> bool:
    """Whether `import name` finds a module outside the snippets."""
    if name in sys.stdlib_module_names:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except ImportError, ValueError:
        return False


def test_no_snippet_shadows_a_module() -> None:
    """No file or folder next to a snippet hides a module it could import.

    `python path/to/snippet.py` puts the snippet's folder first on
    `sys.path`. A `redis.py` or an `http/` beside it then answers
    `import redis` or `import http`, and the snippet fails before it runs.
    """
    folders = {
        (_SNIPPETS_DIR / rel).parent
        for rel in _ALL
        if not rel.endswith("__init__.py")
    }
    shadows = sorted(
        (entry.relative_to(_SNIPPETS_DIR)).as_posix()
        for folder in folders
        for entry in folder.iterdir()
        if not entry.name.startswith(("_", "."))
        and (entry.is_dir() or entry.suffix == ".py")
        and _importable(entry.stem)
    )
    assert not shadows, (
        f"snippet files or folders named like a module: {shadows}. Rename them."
    )


def _loggers() -> list[logging.Logger]:
    """Return the root logger and every named logger created so far."""
    return [
        logging.getLogger(),
        *(
            logger
            for logger in logging.Logger.manager.loggerDict.values()
            if isinstance(logger, logging.Logger)
        ),
    ]


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Undo the logging a snippet sets up, so it cannot reach later tests.

    A snippet shows setup a real app does once, such as adding a filter to
    `grelmicro.health`. Left in place, that filter drops lines a later test
    expects, in whichever test happens to run next.
    """
    saved = {
        id(logger): (
            logger.level,
            list(logger.filters),
            list(logger.handlers),
            logger.propagate,
            logger.disabled,
        )
        for logger in _loggers()
    }
    yield
    for logger in _loggers():
        level, filters, handlers, propagate, disabled = saved.get(
            id(logger), (logging.NOTSET, [], [], True, False)
        )
        logger.setLevel(level)
        logger.filters[:] = filters
        logger.handlers[:] = handlers
        logger.propagate = propagate
        logger.disabled = disabled


@pytest.mark.parametrize("rel", _RUNNABLE)
def test_snippet_imports(rel: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each runnable snippet imports without error."""
    for key, value in _ENV.get(rel, {}).items():
        monkeypatch.setenv(key, value)
    _import_snippet(rel)


def test_snippet_defining_main_runs_it() -> None:
    """A snippet that defines `main()` also calls it.

    Without the call the file runs to the end and prints nothing, so a
    reader who copies it sees an example that does nothing at all.
    """
    silent = [
        rel
        for rel in _ALL
        if _defines_main(rel)
        and not re.search(r"(asyncio|anyio)\.run\(\s*main", _source(rel))
    ]
    assert not silent, (
        f"snippets that define main() and never run it: {silent}. "
        'Add `if __name__ == "__main__": asyncio.run(main())`.'
    )


@pytest.mark.parametrize("rel", _SCRIPT)
def test_snippet_runs_as_script(
    rel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each self-contained snippet runs the way a reader runs it."""
    for key, value in _ENV.get(rel, {}).items():
        monkeypatch.setenv(key, value)
    runpy.run_path(str(_SNIPPETS_DIR / rel), run_name="__main__")


def _fastapi_snippets() -> list[str]:
    return [
        rel
        for rel in _RUNNABLE
        if rel not in _NEEDS_SERVICE and "FastAPI(" in _source(rel)
    ]


_SERVER_ERROR = 500
"""The first status code that says the app itself failed."""


@pytest.mark.parametrize("rel", _fastapi_snippets())
def test_fastapi_snippet_routes_answer(
    rel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every GET route of a FastAPI snippet answers, and none fails.

    Importing the module proves the app builds. It says nothing about
    what the endpoints answer, which is where a missing `install` shows
    up: the route raises `NoActiveAppError` and the reader gets a `500`.
    The app runs under `micro.fake()`, the way its tests would run it.
    """
    for key, value in _ENV.get(rel, {}).items():
        monkeypatch.setenv(key, value)
    module = _import_snippet(rel)
    app = getattr(module, "app", None)
    if app is None:
        pytest.skip(f"{rel} builds no app")
    paths = sorted(
        {
            f"{prefix}{route.path}"
            for prefix, route, _ in walk_routes(app)
            if "GET" in (getattr(route, "methods", None) or set())
            and "{" not in route.path
        }
    )
    micro = getattr(module, "micro", None)
    fake = (
        micro.fake()
        if isinstance(micro, grelmicro.Grelmicro)
        else contextlib.nullcontext()
    )
    with fake, TestClient(app) as client:
        for path in paths:
            response = client.get(path)
            assert response.status_code < _SERVER_ERROR, (
                f"{rel}: GET {path} answered {response.status_code}\n"
                f"{response.text}"
            )


def _needs_a_function(body: str) -> bool:
    """Whether a block only compiles inside an `async` function."""
    try:
        compile(body, "<block>", "exec", dont_inherit=True)
    except SyntaxError as error:
        return bool(_OUTSIDE_FUNCTION_RE.search(error.msg))
    return False


@pytest.mark.parametrize(
    "body",
    [
        "await sleep()",
        "x = await sleep()",
        "if await ready():\n    pass",
        "try:\n    async with lock:\n        pass\nfinally:\n    pass",
        "return 1",
        "[x async for x in items()]",
    ],
)
def test_needs_a_function_catches_every_spelling(body: str) -> None:
    """A top-level `await`, `async with`, `async for` or `return` is caught."""
    assert _needs_a_function(body)


def test_needs_a_function_passes_a_whole_block() -> None:
    """A block that compiles on its own needs no fragment label."""
    assert not _needs_a_function("async def main():\n    await sleep()")


def test_inline_fragment_blocks_are_marked() -> None:
    """A block that only runs inside an `async` function says so.

    A reader copies a block as written. One with a top-level `await` or
    `return` raises `SyntaxError` in a file of its own, so the page has to say it
    is a piece of something bigger.
    """
    unmarked = [
        f"{page.relative_to(_ROOT)}:{line}"
        for page in _pages()
        for line, info, body in _blocks(page)
        if info.startswith("python")
        and 'title="fragment"' not in info
        and _needs_a_function(body)
    ]
    assert not unmarked, (
        f"python blocks that only run inside a function and carry no "
        f"fragment label: "
        f'{unmarked}. Wrap them in a function or add `title="fragment"`.'
    )


def _missing_imports(body: str) -> list[str]:
    """Grelmicro names a self-contained block uses and never imports."""
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return []
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    if not imports:
        return []
    bound = {
        (alias.asname or alias.name.split(".")[0])
        for node in imports
        for alias in node.names
    }
    for node in ast.walk(tree):
        if isinstance(
            node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    used = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    return sorted((used & _PUBLIC_NAMES) - bound)


def test_missing_imports_counts_parameters_and_caught_errors() -> None:
    """A parameter or an `except ... as` name is defined by the block."""
    body = textwrap.dedent(
        """
        import logging


        def emit(record):
            return record


        try:
            pass
        except Exception as record:
            print(record)
        """
    )
    assert "record" in _PUBLIC_NAMES
    assert _missing_imports(body) == []


def test_inline_blocks_import_the_names_they_use() -> None:
    """A block that imports anything imports every grelmicro name it uses.

    A block with no import at all continues the one above it. One that
    opens with imports reads as a whole file, so a name it never imports
    is a `NameError` for whoever copies it.
    """
    offenders = {
        f"{page.relative_to(_ROOT)}:{line}": missing
        for page in _pages()
        for line, info, body in _blocks(page)
        if info.startswith("python") and (missing := _missing_imports(body))
    }
    assert not offenders, (
        f"blocks using grelmicro names they never import: {offenders}"
    )


_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})?"
)
_HEX_ID_RE = re.compile(r"\b[0-9a-f]{16,32}\b")
_DURATION_RE = re.compile(r"\b\d+\.\d+s\b")


def _normalise(text: str) -> str:
    """Take out what changes between two runs of the same snippet."""
    text = textwrap.dedent(text)
    text = _TIMESTAMP_RE.sub("<time>", text)
    text = _HEX_ID_RE.sub("<id>", text)
    text = _DURATION_RE.sub("<duration>", text)
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def _output_pairs() -> list[tuple[str, str, str]]:
    """Each `title="output"` block with the snippet above it."""
    pairs = []
    for page in _pages():
        snippet = None
        for line, info, body in _blocks(page):
            include = _INCLUDE_RE.search(body)
            if include:
                snippet = include.group(1)
            elif 'title="output"' in info and snippet:
                pairs.append(
                    (f"{page.relative_to(_DOCS_DIR)}:{line}", snippet, body)
                )
    return pairs


_COLOR_VARIABLES = frozenset(
    {
        "FORCE_COLOR",
        "NO_COLOR",
        "CLICOLOR",
        "CLICOLOR_FORCE",
        "TERM",
        "COLORTERM",
    }
)
"""Variables that turn terminal colors on or off, left out of a snippet run."""


def _run_snippet(rel: str, extra: dict[str, str] | None = None) -> str:
    """Run a snippet as a reader does, with its documented environment."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _COLOR_VARIABLES
    }
    env.update(_ENV.get(rel, {}))
    env.update(extra or {})
    completed = subprocess.run(  # noqa: S603
        [sys.executable, str(_SNIPPETS_DIR / rel)],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        check=False,
        timeout=120,
        env=env,
    )
    assert completed.returncode == 0, (
        f"{rel} exited {completed.returncode}\n{completed.stderr}"
    )
    return completed.stdout


_LOCAL_OUTPUT = [
    pair for pair in _output_pairs() if pair[1] not in _NEEDS_SERVICE
]
_POSTGRES_OUTPUT = [
    pair
    for pair in _output_pairs()
    if _NEEDS_SERVICE.get(pair[1]) == "Postgres"
]


def _assert_prints(where: str, rel: str, expected: str, printed: str) -> None:
    """Match what a snippet printed against the block a page shows.

    Times, identifiers and durations change on every run, so they are
    normalised on both sides, and `...` in the page matches anything.
    """
    actual = _normalise(printed)
    pattern = re.escape(_normalise(expected)).replace(r"\.\.\.", ".*?")
    assert re.fullmatch(pattern, actual, re.DOTALL), (
        f"{where}: {rel} prints\n{actual}\n\nthe page shows\n{expected}"
    )


def test_every_output_block_is_run() -> None:
    """An output block needs a service the tests start, or none."""
    unrun = [
        where
        for where, rel, _ in _output_pairs()
        if rel in _NEEDS_SERVICE and _NEEDS_SERVICE[rel] != "Postgres"
    ]
    assert not unrun, f"output blocks no test runs: {unrun}"


@pytest.mark.parametrize(
    ("where", "rel", "expected"),
    _LOCAL_OUTPUT,
    ids=[where for where, _, _ in _LOCAL_OUTPUT],
)
def test_documented_output_matches(where: str, rel: str, expected: str) -> None:
    """A page that shows output shows what the snippet really prints."""
    _assert_prints(where, rel, expected, _run_snippet(rel))


@pytest.mark.integration
@pytest.mark.timeout(CONTAINER_LOG_TIMEOUT + 120)
@pytest.mark.parametrize(
    ("where", "rel", "expected"),
    _POSTGRES_OUTPUT,
    ids=[where for where, _, _ in _POSTGRES_OUTPUT],
)
def test_documented_output_matches_on_postgres(
    where: str, rel: str, expected: str
) -> None:
    """A snippet that needs Postgres prints what its page shows."""
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        url = f"postgresql://test:test@localhost:{port}/test"
        printed = _run_snippet(rel, {"POSTGRES_URL": url})
    _assert_prints(where, rel, expected, printed)


@pytest.mark.parametrize(
    "rel", sorted(rel for rel in _ENV if rel not in _NEEDS_SERVICE)
)
def test_run_snippet_sets_the_documented_environment(rel: str) -> None:
    """A snippet that reads its settings from the environment runs.

    Each of these fails at start without the variables its page
    documents, so exiting cleanly proves the output comparison sets them.
    """
    _run_snippet(rel)


_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]")


def _headings(page: Path) -> list[int]:
    """Return the line numbers of the page's headings, outside fenced blocks."""
    text = page.read_text()
    fenced: set[int] = set()
    for match in _FENCE_RE.finditer(text):
        first = text[: match.start()].count("\n") + 1
        last = text[: match.end()].count("\n") + 1
        fenced.update(range(first, last + 1))
    return [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if number not in fenced and _HEADING_RE.match(line)
    ]


def test_output_blocks_are_marked() -> None:
    """A block a page calls output is checked against the snippet.

    The label is what runs the comparison, so a page claiming output in
    an unlabelled block would drift back without a word.
    """
    unmarked = []
    for page in _pages():
        lines = page.read_text().splitlines()
        headings = _headings(page)
        include_line = None
        for line, info, body in _blocks(page):
            if _INCLUDE_RE.search(body):
                include_line = line
                continue
            if include_line is None or any(
                include_line < heading < line for heading in headings
            ):
                include_line = None
                continue
            above = lines[max(line - 2, 0) : line - 1]
            lead = above[0].strip() if above else ""
            says_output = bool(
                re.fullmatch(r"[\w ]*output:", lead, re.IGNORECASE)
            )
            if says_output and 'title="output"' not in info:
                unmarked.append(f"{page.relative_to(_ROOT)}:{line}")
    assert not unmarked, (
        f'blocks shown as output without `title="output"`: {unmarked}. '
        "Label it so the snippet's real output is compared against it."
    )
