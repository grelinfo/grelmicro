# Import Strategy

grelmicro follows the Django/SQLAlchemy pattern: **core API is re-exported from package `__init__.py`, backends are imported from their submodules.**

## Principle

| Import from | Contains |
|-------------|----------|
| Package (`grelmicro.coordination`) | Primitives, protocols, errors, types (what every user needs) |
| Submodule (`grelmicro.coordination.redis`) | Backend implementations (infrastructure-specific code) |

## Rationale

**No unnecessary imports.** Importing a primitive does not pull in backend client libraries. Users only pay for the backend they choose.

**Explicit dependencies.** A submodule import like `from grelmicro.coordination.redis import RedisLockAdapter` makes the infrastructure dependency visible at the import site. Grepping for the submodule path finds every file that needs that backend.

**Ecosystem convention.** Django databases, SQLAlchemy dialects, and Celery brokers all follow this pattern. Backends are selected once in configuration, not scattered across business logic.

## Top-level re-exports

Patterns are imported from their submodule (`from grelmicro.coordination import Lock`), never from the package root. The submodule path is the classification: it names the docs page, the Component, and the backend family. A flat top-level namespace was evaluated for 1.0 and rejected, one obvious import per task beats one saved line.
