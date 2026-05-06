# Concurrency runtime

grelmicro targets `asyncio` directly. Trio and the AnyIO compatibility layer are not supported.

## Why asyncio only

asyncio is in the standard library. Every Python web framework grelmicro is meant to plug into (FastAPI, Starlette, Litestar, Sanic, AIOHTTP) runs on asyncio. Targeting one runtime keeps the bridge code small: backends capture the running loop in `__aenter__` and the sync adapters dispatch through `asyncio.run_coroutine_threadsafe` with no abstraction in the hot path. See [Sync from thread](sync-from-thread.md) for how that bridge works.

## What this means in practice

- Use `asyncio.run(main())` or `uvloop.run(main())`. The `standard` extra ships uvloop on Linux and macOS.
- Sync code that needs to call a primitive uses the per-component sync adapter (`lock.from_thread`, `cb.from_thread`, `@cached(...)`). It does not need a separate runtime bridge.
- grelmicro does not import or depend on AnyIO directly. AnyIO may still be present in the environment transitively (for example through `fast-depends`), but no grelmicro code uses it.
- The test suite runs on `pytest-asyncio` with `asyncio_mode = "auto"`.
