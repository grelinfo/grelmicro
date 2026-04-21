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

## Branch and commit conventions

- Work on a branch named for the change, e.g. `feat/<name>` or
  `fix/<name>`.
- Every commit and PR title **must** start with a
  [gitmoji](https://gitmoji.dev/) plus a
  [conventional-commit](https://www.conventionalcommits.org/) prefix:
  - `✨ feat(<scope>): ...`
  - `🐛 fix(<scope>): ...`
  - `📝 docs(<scope>): ...`
  - `🔧 ci: ...`
  - `♻️ refactor(<scope>): ...`
  - `🗑️ chore: ...`
- Reference the issue or PR number in parentheses where applicable.
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
- Keep `__init__` bodies thin. Attribute assignment only; delegate
  validation to a frozen Pydantic config model.

### Pydantic models

- Every configuration class is
  `BaseModel, frozen=True, extra="forbid"`.
- Every field is annotated with `Annotated[type, Doc("...")]`.
- For discriminated unions, include a `type: Literal["..."]`
  discriminator field and wrap the union with
  `Annotated[A | B, Discriminator("type")]`.
- Expose the config through a `@property def config(self) -> XConfig`
  on the front-door class.

### Naming

- Full English words. No `algo`, `ctx`, `cfg`. The one exception
  is the underscore-prefixed private module names
  (`_protocol.py`, `_backends.py`).
- Public primitives get short, descriptive class names
  (`RateLimiter`, `Lock`, `CircuitBreaker`).
- Vendor- or product-specific names do **not** appear in public
  docstrings or user-facing docs. Industry conventions are OK
  ("token bucket", "sliding window"); brand names are not.
  Attribution of adapted code belongs in `THIRD_PARTY_NOTICES.md`.

### Error messages

- Always actionable; include the offending value where relevant
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
  shortened to the local name; the target path must be fully
  qualified.
- **External links** use plain markdown: `[Label](url)`.
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

### Example (a public class)

```python
class RateLimiter:
    """Rate limiter with a pluggable algorithm.

    Summary paragraph.

    Second paragraph explaining a subtle constraint.

    Example:
    ```python
    from grelmicro.resilience import RateLimiter, TokenBucket

    rl = RateLimiter("api", algorithm=TokenBucket(capacity=10, refill_rate=1))
    ```

    Read more in the [Resilience](../resilience.md) docs.
    """
```

## Testing

- Name tests for the behaviour, not the method
  (`test_acquire_rejected_when_limit_exceeded`).
- Every test function has a one-line docstring.
- Use **Arrange / Act / Assert** comments to separate phases.
- Parametrize related cases with `pytest.mark.parametrize`.
- Mark integration tests (those that require Docker / a live
  service) with `pytest.mark.integration`; unit tests run by
  default.
- Prefer fixtures with a `_` prefix for side-effect-only setups
  (consumed via `@pytest.mark.usefixtures`). Fixtures returning a
  value the test uses drop the prefix.
- **100 % coverage** across unit + integration is enforced.

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
4. Never use vendor-specific names in API documentation; keep
   attribution in `THIRD_PARTY_NOTICES.md` only.

## Architectural conventions

### Backends

- Every storage-agnostic primitive (`Lock`, `RateLimiter`, cache,
  ...) has a matching `XBackend` Protocol under the package's
  `_protocol.py`, with at least a Memory and a Redis
  implementation.
- Backends are registered in a shared registry
  (`grelmicro/_backends.py::BackendRegistry`); user code picks a
  backend either by initialising a concrete backend class with
  `auto_register=True` or by passing `backend=` explicitly to the
  primitive's constructor.
- Primitives expose the `backend=` override even when they also
  fall back to the registry.

### Smooth deprecations (pre-1.0 policy)

grelmicro is still on a `0.x` series. The explicit goal is to
reach **1.0.0 with a stable, well-shaped API**, not to carry a
long `0.x` tail. We follow the Pydantic playbook, where aggressive
pre-1.0 deprecations produced a clean v1/v2, rather than the
"endless 0.x minor bumps with full back-compat" approach some other
projects take.

Concretely:

- Pre-1.0 we iterate on the public API freely. If a shape can be
  improved, change it. Don't let "might break users" block a
  better long-term design.
- Every change still ships through a **smooth deprecation cycle**
  so users get one release to migrate:
  - Keep the old signature working and emit a `DeprecationWarning`
    that names the new API and the removal version.
  - Mark the old parameter with
    [`typing_extensions.deprecated`](https://peps.python.org/pep-0702/)
    so type-checkers and IDEs flag it inline.
  - Reference the removal version in both the `DeprecationWarning`
    message and the `Doc(...)` block.
- **Target removal one minor release after the deprecation ships.**
  For example, deprecated in `0.14.0` → removed in `0.15.0`.
- Keep a "Deprecated" entry in `docs/changelog.md` under the
  minor that introduces the deprecation **and** under the minor
  that removes it.

Post-1.0 this policy tightens (longer deprecation windows, no
breaking changes outside major releases), but we're not there
yet. If you're unsure whether a change is "aggressive enough",
open an issue and ask; a slightly worse 0.x migration path is
cheaper than a bad API we carry for years.

## Before opening a PR

- All pre-commit gates pass locally
  (`uv run pre-commit run --all-files`).
- Coverage stays at 100 %.
- Every new public symbol has a docstring and a test.
- `docs/` is updated if user-facing behaviour changed.
- `docs/changelog.md` has an entry under `## Unreleased`.
- Commit titles follow the gitmoji + conventional-commit format.

Thanks for reading. If a convention in this document surprised
you, open an issue: either the rule is wrong or the rationale
isn't written down yet.
