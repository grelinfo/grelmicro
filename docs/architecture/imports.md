# Import Strategy

grelmicro follows the Django/SQLAlchemy pattern: **core API is re-exported from package `__init__.py`, backends are imported from their submodules.**

## Principle

| Import from | Contains |
|-------------|----------|
| Package (`grelmicro.sync`) | Primitives, protocols, errors, types (what every user needs) |
| Submodule (`grelmicro.sync.redis`) | Backend implementations (infrastructure-specific code) |

## Rationale

**No unnecessary imports.** Importing a primitive does not pull in backend client libraries. Users only pay for the backend they choose.

**Explicit dependencies.** A submodule import like `from grelmicro.sync.redis import RedisSyncBackend` makes the infrastructure dependency visible at the import site. Grepping for the submodule path finds every file that needs that backend.

**Ecosystem convention.** Django databases, SQLAlchemy dialects, and Celery brokers all follow this pattern. Backends are selected once in configuration, not scattered across business logic.
