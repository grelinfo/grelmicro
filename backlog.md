# Redis Cache Backend

## Phase 1: AsyncCache Protocol + Decorator Update

- [x] Add `AsyncCache` protocol to `grelmicro/cache/_protocol.py`
- [x] Update `@cached` decorator in `grelmicro/cache/cached.py`
- [x] Add `AsyncCache` to `grelmicro/cache/__init__.py` exports
- [x] Tests: `tests/cache/test_cached_async_cache.py`

## Phase 2: RedisCache Implementation

- [x] Create `grelmicro/cache/redis.py`
- [x] Add `auto_register` support via shared `BackendRegistry`

## Phase 3: Testing

- [x] Unit tests: `tests/cache/test_redis.py`
- [x] Integration tests: `tests/cache/test_cache_backends.py`

## Phase 4: Documentation

- [x] Update `docs/cache.md` with RedisCache usage
- [x] Update `docs/reference/cache.md` API reference
- [x] Add `docs/architecture/backends.md` shared backend registry

## Bonus: Shared Backend Registry

- [x] Create `grelmicro/_backends.py` with generic `BackendRegistry[T]`
- [x] Refactor `sync/_backends.py` to use shared registry
- [x] Update all 5 sync backends and 11 test cleanups
- [x] Add `cache/_backends.py` with `cache_backend_registry`
