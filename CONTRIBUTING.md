# Contributing to grelmicro

Thank you for your interest in contributing. This document captures
the conventions the project follows so your pull request lands
quickly and cleanly.

grelmicro aims for the same level of polish and documentation
ergonomics as [FastAPI](https://fastapi.tiangolo.com/) and
[Pydantic](https://docs.pydantic.dev/). When in doubt, imitate those
two projects.

## Quick start

```bash
# Install with all extras for development
uv sync --all-extras

# Install the pre-commit hooks (runs all CI gates on every commit)
uv run pre-commit install

# Run the full gate set on demand
uv run pre-commit run --all-files
```

The pre-commit configuration (`.pre-commit-config.yaml`) is the
single source of truth for what "passes CI": ruff, ty, pytest
(unit and integration), coverage, and file-hygiene hooks.

## Your first contribution

Browse [issues labelled `good first issue`](https://github.com/grelinfo/grelmicro/issues?q=is%3Aopen+is%3Aissue+label%3A%22good+first+issue%22).
They are scoped so a contributor unfamiliar with the codebase can
finish them in one sitting.

A typical first contribution touches three places:

- **Code** in `grelmicro/<module>/...`. Public functions and
  parameters use `Annotated[..., Doc("one-line behavior")]`.
- **Test** in `tests/<module>/test_<thing>.py`. One docstring per
  test, Arrange / Act / Assert comments, name describes the
  scenario and expected outcome.
- **Docs** in `docs/<module>.md` and a runnable example under
  `docs/snippets/<module>/`. The snippet must run on its own.

Example shapes (copy and adapt):

```python
# grelmicro/<module>/<thing>.py
from typing import Annotated
from typing_extensions import Doc


class Counter:
    """Monotonic event counter."""

    def __init__(
        self,
        *,
        initial: Annotated[
            int,
            Doc("Starting value. Must be `>= 0`."),
        ] = 0,
    ) -> None:
        self._value = initial

    @property
    def value(self) -> int:
        """Current counter value."""
        return self._value

    def increment(
        self,
        amount: Annotated[
            int,
            Doc("Increment step. Must be `> 0`."),
        ] = 1,
    ) -> None:
        """Increase the counter by `amount`."""
        self._value += amount
```

```python
# tests/<module>/test_<thing>.py
from grelmicro.<module>.<thing> import Counter


def test_counter_increment_advances_value() -> None:
    """`increment` advances the value by the given amount."""
    # Arrange
    counter = Counter(initial=10)
    # Act
    counter.increment(amount=5)
    # Assert
    assert counter.value == 15
```

A `docs/changelog.md` entry under `## Unreleased` is required.
One short bullet, the PR link at the end. Match the surrounding
entries.

If anything is unclear, open the PR as a draft and ask. Reviewing
a partially-correct PR is faster than guessing.

## Branch and commit conventions

- Work on a branch named for the change, e.g. `feat/<name>` or
  `fix/<name>`.
- Every commit and PR title **must** follow the
  [gitmoji](https://gitmoji.dev/) convention. The format is a single
  emoji followed by a short imperative sentence:

  ```
  <emoji> <message>
  ```

  - `emoji`: one emoji from the [gitmoji list](https://gitmoji.dev/)
    that describes what the change does. The emoji replaces the type
    keyword, so there is no `feat:`, `fix:`, or `docs:` and no scope
    in parentheses. Pick the emoji whose meaning fits best.
  - `message`: a concise imperative summary, kept to one line.

  The [full gitmoji list](https://gitmoji.dev/) is the source of
  truth. The ones that cover most grelmicro commits, with the
  exact code and description from gitmoji.dev:

  | Emoji | `:code:` | Description (from gitmoji.dev) |
  |---|---|---|
  | ✨ | `:sparkles:` | Introduce new features. |
  | 🐛 | `:bug:` | Fix a bug. |
  | 📝 | `:memo:` | Add or update documentation. |
  | ♻️ | `:recycle:` | Refactor code. |
  | ✅ | `:white_check_mark:` | Add, update, or pass tests. |
  | ⚡️ | `:zap:` | Improve performance. |
  | 🔒️ | `:lock:` | Fix security or privacy issues. |
  | 👷 | `:construction_worker:` | Add or update CI build system. |
  | ⬆️ | `:arrow_up:` | Upgrade dependencies. |
  | 🚨 | `:rotating_light:` | Fix compiler / linter warnings. |
  | 🔥 | `:fire:` | Remove code or files. |
  | 🎨 | `:art:` | Improve structure / format of the code. |

  Sample titles in the current style (emoji, then an imperative
  sentence, no type keyword or scope):

  - `✨ Add DuplicateFilter for noisy repeated log records`
  - `🔒 Grant security-events write to the workflow lint job`
  - `👷 Add attestations and wheel verification to the release`
  - `📝 Update the changelog for the next release`
  - `♻️ Bind the algorithm to the strategy once at construction`
  - `⬆️ Bump pydantic-extra-types from 2.11.1 to 2.11.2`

- The PR number is appended automatically on squash merge
  (`... (#123)`), so you do not write it in the title yourself.
- Keep each commit focused. When chaining related commits, pause
  for review between them.

## Code style

### Type annotations

Mechanical style rules (modern unions, built-in generics,
keyword-only arguments, import layout) are enforced by ruff and
ty: let the tooling fail the hook and fix what it points at.
The rules below are the ones the tooling can't check:

- Annotate **every public parameter** with
  [`typing.Annotated`](https://docs.python.org/3/library/typing.html#typing.Annotated)
  and
  [`typing_extensions.Doc`](https://peps.python.org/pep-0727/).
  Each `Annotated` block reads as a self-contained paragraph of
  documentation.
- Mark deprecated parameters with
  [`typing_extensions.deprecated`](https://peps.python.org/pep-0702/)
  as a second `Annotated` entry so type-checkers and IDEs surface
  the deprecation inline. Keep a runtime `warnings.warn` alongside
  it.
- Keep `__init__` bodies thin. Attribute assignment only. Delegate
  validation to a frozen Pydantic config model.

### Comments

Document what a symbol is and why it exists **on the symbol**, the way
FastAPI does, not in a block of `#` comments above it. A reader hovering
the symbol in an IDE, and mkdocstrings, should see the explanation.

- For parameters and fields, use `Annotated[type, Doc("...")]`.
- For a class or function, use its docstring.
- For a module constant or class attribute, use a `Doc(...)` annotation
  on the annotated assignment, or an attribute docstring (a string
  literal directly under the assignment).
- Reserve `#` comments for short notes about non-obvious local logic
  inside a function body. Do not use them to describe a public or
  module-level symbol.

Avoid (an explanatory `#` block describing a constant):

```python
# Advisory-lock namespace for the rate limiter. hashtextextended is
# PG's 64-bit text hash with a configurable seed. A distinct seed
# isolates rate-limiter keys from any other advisory lock.
_RATE_LIMITER_ADVISORY_NAMESPACE = 0x67726C72_72746C6D
```

Prefer (an attribute docstring the symbol carries):

```python
_RATE_LIMITER_ADVISORY_NAMESPACE = 0x67726C72_72746C6D
"""Advisory-lock namespace for the rate limiter.

`hashtextextended` is Postgres's 64-bit text hash with a configurable
seed. A distinct seed isolates rate-limiter keys from any other
advisory lock in the same database.
"""
```

### Pydantic models

- Every configuration class is
  `BaseModel, frozen=True, extra="forbid"`.
- Every field is annotated with `Annotated[type, Doc("...")]`.
- For discriminated unions, include a `kind: Literal["..."]`
  discriminator field and wrap the union with
  `Annotated[A | B, Discriminator("kind")]`. `kind` avoids
  shadowing the Python `type` builtin on every config object.
- Expose the config through a `@property def config(self) -> XConfig`
  on the front-door class.

### Naming

- Full English words. No `algo`, `ctx`, `cfg`. The one exception
  is the underscore-prefixed private module names
  (`_protocol.py`, `_component.py`).
- Public primitives get short, descriptive class names
  (`RateLimiter`, `Lock`, `CircuitBreaker`).
- Vendor- or product-specific names do **not** appear in public
  docstrings or user-facing docs. Industry conventions are OK
  ("token bucket", "sliding window"). Brand names are not.
  Attribution of adapted code belongs in `THIRD_PARTY_NOTICES.md`.

### Error messages

- Always actionable. Include the offending value where relevant
  (`f"cost must be between 1 and {limit}, got {cost}"`).
- Domain errors inherit from a package-specific base
  (`ResilienceError`, `LoggingError`, `SyncError`) which itself
  inherits from `GrelmicroError`.
- Use
  [`typing.assert_never`](https://docs.python.org/3/library/typing.html#typing.assert_never)
  in `match` statements over discriminated unions so new variants
  fail type-check immediately.

### Runtime-cost discipline

- A primitive with a pluggable strategy or algorithm should
  resolve the choice **once at construction** (bind it to a
  concrete object) and forward directly on every call. No
  `isinstance` / `match` dispatch on the hot path.
- Sync code paths that will be called from loops or
  `logging.Filter.filter()` must avoid allocation and I/O where
  possible. Prefer `time.monotonic` to `time.time`.

## Docstring style

We use [MkDocs Material](https://squidfunk.github.io/mkdocs-material/)
with [mkdocstrings](https://mkdocstrings.github.io/) and the Google
docstring style.

### Conventions

- **Markdown only.** No reStructuredText directives
  (`:class:`, `:meth:`, `:func:`, `:mod:`).
- **Single backticks** (`` `Name` ``) for inline code and type
  references. Double backticks are not required.
- **Cross-references** use mkdocstrings syntax:
  `` [`Name`][grelmicro.full.path.Name] ``. The label may be
  shortened to the local name. The target path must be fully
  qualified.
- **External links** use plain markdown: `[Label](url)`.
- **RFC and PEP references** link to the official page in
  Markdown files under `docs/`: `[RFC 9211](https://www.rfc-editor.org/rfc/rfc9211.html)`
  and `[PEP 702](https://peps.python.org/pep-0702/)`. Inside
  Python docstrings, keep them as plain text (`RFC 9211`,
  `PEP 702`) so they read naturally in IDEs and tracebacks.
- **Runnable example** in every public class docstring. Include
  all necessary `import` statements and show the primitive in use.
- **"Read more" link** at the end of class docstrings, pointing
  to the section of the user guide that covers the feature:
  `` Read more in the [Topic](../topic.md#section) docs. ``
- **Google-style `Args:` / `Returns:` / `Raises:` sections** on
  method docstrings. `__init__` docstrings stay as a one-line
  summary because the parameter docs live in the `Annotated`
  `Doc(...)` blocks.
- **Triple-quoted multi-paragraph `Doc(...)`** blocks with blank
  leading and trailing lines. Single-paragraph `Doc(...)` may use
  one line of prose.
- **No em dashes.** Use `: `, ` - ` (spaced), or `(…)` to separate
  clauses. Rewrite the sentence if none of those fit.
- **No semicolons in prose.** Split into two sentences or use a
  comma. Semicolons remain fine inside code blocks, SQL snippets,
  and parametrize IDs.

### Plain English for non-native readers

A large share of grelmicro's readers are professional developers
whose first language is not English. Write accordingly. This
applies to every user-facing surface: Markdown pages under `docs/`,
docstrings that mkdocstrings renders, the README, and release
notes.

- **Plain, industry-standard vocabulary.** Use the words readers
  already know from FastAPI, Pydantic, AWS, Kubernetes, and the
  Python standard library. Prefer "backend", "primitive", "request
  path", "timeout", "retry", "deadline" over invented or clever
  alternatives.
- **Short active sentences.** Aim for 15-20 words. Split rather
  than stack clauses with "and", "which", or a comma.
- **No idioms or metaphors.** Examples to avoid: "plumbing",
  "eating threads", "hitting a key", "touching a lock", "gate the
  task behind", "boring in the best possible way", "à la carte",
  "surface the health", "in the spirit of". Literal verbs win.
- **No anthropomorphic phrasing.** "The primitive asks for...",
  "the backend speaks a protocol..." - rewrite with a literal
  subject ("The primitive needs...", "the backend implements a
  protocol...").
- **No marketing adjectives without support.** Drop "effortlessly",
  "aggressively", "ergonomic", "blazing", "best possible way". If
  you claim something is fast or production-ready, either point to
  a benchmark or test suite, or rewrite with the concrete feature
  that delivers it.
- **Name technologies we integrate with** (FastAPI, Redis,
  PostgreSQL, Kubernetes, OpenTelemetry). Do not drop vendor
  names for flavour or comparison ("like <other library>",
  "in the <vendor> style"). Industry concept names (JSON, sliding window,
  token bucket) are fine.
- **Compact shorthand is fine inside code blocks** (`~3x`, `1:1`,
  `1/sec`), but expand it in prose ("about 3 times faster",
  "maps directly", "1 per second").

### Example (a public class)

```python
class RateLimiter:
    """Rate limiter with a pluggable algorithm.

    Summary paragraph.

    Second paragraph explaining a subtle constraint.

    Example:
    ```python
    from grelmicro.resilience import RateLimiter

    rl = RateLimiter.token_bucket("api", capacity=10, refill_rate=1)
    ```

    Read more in the [Rate Limiter](../resilience/rate-limiter.md) docs.
    """
```

## Testing

- Name tests with the shape
  `test_<component>_<scenario>_<expected_outcome>`. For example:
  `test_ratelimiter_acquire_rejected_when_limit_exceeded`,
  `test_cache_get_returns_none_when_expired`,
  `test_lock_release_raises_when_not_owned`.
  Existing tests that follow an older shape are migrated opportunistically.
- Every test function has a one-line docstring.
- Use **Arrange / Act / Assert** comments to separate phases.
- Parametrize related cases with `pytest.mark.parametrize`.
- Mark integration tests (those that require Docker / a live
  service) with `pytest.mark.integration`. Unit tests run by
  default.
- Prefer fixtures with a `_` prefix for side-effect-only setups
  (consumed via `@pytest.mark.usefixtures`). Fixtures returning a
  value the test uses drop the prefix.
- **100 % line + branch coverage** across unit + integration is
  enforced by the pre-commit `coverage-report` hook
  (`coverage report --fail-under=100`). Pull requests and pushes to
  `main` run the unit tier. The nightly schedule and releases run the
  full unit, slow, and integration suite.

### Running tests

```bash
# Unit tests only (no Docker required)
uv run pytest -m "not integration"

# Integration tests (requires Docker for Redis and Postgres containers)
uv run pytest -m integration

# Full suite with combined coverage (matches the local pre-commit gate)
uv run pytest -m "not integration" --cov
uv run pytest -m integration --cov --cov-append
uv run coverage report --fail-under=100
```

## Documentation

- Every public module, class, method, and dataclass field has a
  docstring.
- Every user-facing feature has a section in the relevant
  `docs/*.md` page and a runnable snippet under
  `docs/snippets/<topic>/`.
- Snippets must be **self-contained and runnable**: include
  every `import` and define every variable they reference.
- Cross-reference classes with mkdocstrings syntax so the docs
  site auto-links to the API reference.
- When adding a decision point (e.g. multiple algorithms), write
  an explicit **"Choosing a ..." guide**: a numbered decision
  list plus a side-by-side comparison table.

## Third-party material

If you adapt code from another project, even a snippet:

1. Credit the source in `THIRD_PARTY_NOTICES.md` with a link to
   the original repo and its copyright line.
2. Verify the licence is compatible with MIT. Both the source
   repo and our root `LICENSE` must remain consistent.
3. Add a comment near the adapted code pointing to the
   `THIRD_PARTY_NOTICES.md` section.
4. Never use vendor-specific names in API documentation. Keep
   attribution in `THIRD_PARTY_NOTICES.md` only.

## Architectural conventions

### Providers, adapters, and components

- Every storage-agnostic primitive (`Lock`, `RateLimiter`, cache,
  ...) has a matching `XBackend` Protocol under the package's
  `_protocol.py`, with at least a Memory and a Redis adapter.
- A concrete backend is an **adapter** named `<Vendor><Kind>Adapter`
  (`RedisLockAdapter`, `MemoryCacheAdapter`). A **provider**
  (`RedisProvider`, `PostgresProvider`, `MemoryProvider`) owns a
  vendor connection and builds the matching adapters through factory
  methods (`redis.lock()`, `redis.cache()`).
- A **component** (`Coordination`, `Cache`, `RateLimiterRegistry`,
  ...) wraps one kind of backend. The user wires everything through
  the app: `Grelmicro(uses=[provider, Component(...)])`. Construction
  is pure: `__init__` performs no I/O and no global writes. The app
  opens every registered item as one async context manager.
- Inside `async with micro:` (or under the FastAPI and FastStream
  integrations in `grelmicro.integrations`), a primitive that omits an
  explicit backend resolves the active app through
  `Grelmicro.current()`, so handlers and tasks find their backend
  without extra wiring.

### About grelmicro versions

grelmicro follows [Semantic Versioning](https://semver.org) and is in
its `0.x` phase.

In `0.x`, the minor is the breaking position: a `0.x.0` release may
change the public API, and a `0.x.y` release is a safe patch. The
public API is feature-complete and guarded by a snapshot test
(`tests/test_public_api.py`), so an accidental change to the public
surface fails CI. Every intentional break appears under **Breaking** in
[`docs/changelog.md`](docs/changelog.md) with a migration note.

`1.0` is reserved for the point where the public API is promised
stable. It is a maturity milestone, not a calendar or launch event. We
cross it when the API has held steady and we are ready to commit to it,
not before.

After `1.0`, standard semver applies: breaking changes wait for the
next `MAJOR`, and a removal goes through a `DeprecationWarning` cycle
first. There is no fixed schedule for major versions, and a major
version is never bumped just because time has passed.

## Issues and releases

grelmicro ships continuously: a PR merges, a release follows when
it makes sense. There is no project board and no per-release
milestone.

The full workflow uses **GitHub issue state plus two labels**:

| State | Meaning |
|---|---|
| Open, no label | Backlog. Anyone can propose work here. |
| `next` label | Picked for the next release. Short list. |
| `v1.0` label | Targeted for the 1.0 milestone, not the next release. |
| Assigned to someone | In progress. |
| Closed | Done. |

Useful filters:

```text
is:open is:issue label:next                 # next up
is:open is:issue assignee:@me               # in progress
is:open is:issue no:label                   # groomable backlog
is:open is:issue label:v1.0                 # 1.0 scope
```

A release happens when one or more `next`-labeled issues are
closed and the changelog has enough to ship. There is no
fixed cadence.

## Pull requests

`main` is protected, so every change lands through a pull request.

- **No direct pushes.** Branch from `main`, push your branch, and open
  a pull request.
- **CI must be green.** A single `CI Green` check rolls up lint, types,
  docs, and tests. It is the only required status check.
- **One approving review** is required, and a new push dismisses a
  stale approval.
- **Resolve every conversation** before merge.
- Merges are **squash** only, so `main` keeps a linear history.

CI is tiered to keep feedback fast. Pull requests and pushes to `main`
run the light tier (unit tests on the current Python). The nightly
schedule and every release run the full tier (the Python matrix plus
the slow, integration, and demo tiers), so a release is always tested
against everything before it publishes.

## Before opening a PR

- All pre-commit gates pass locally
  (`uv run pre-commit run --all-files`).
- Coverage stays at 100 %.
- Every new public symbol has a docstring and a test.
- `docs/` is updated if user-facing behaviour changed.
- `docs/changelog.md` has an entry under `## Unreleased`.
- Commit titles follow the gitmoji convention (one emoji and an
  imperative sentence, no type keyword or scope).

Thanks for reading. If a convention in this document surprised
you, open an issue: either the rule is wrong or the rationale
isn't written down yet.
