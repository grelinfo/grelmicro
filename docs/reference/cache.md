# Cache

- **Start here**: [Cache guide](../cache/index.md)
- **Common recipes**: [`@cached`](../cache/cached.md), [`TTLCache`](../cache/index.md#ttlcache)
- **Configuration**: [Backend setup](../cache/index.md#backend), [Redis backend configuration](../providers/redis.md#environment-variables)

::: grelmicro.cache
    options:
      show_submodules: true
      members:
        - CacheBackend
        - CacheError
        - CacheInfo
        - CacheSerializer
        - CachedFunction
        - CachedStream
        - JsonSerializer
        - PickleSerializer
        - PydanticSerializer
        - TTLCache
        - cached

::: grelmicro.cache.memory
    options:
      members:
        - MemoryCacheAdapter

::: grelmicro.cache.redis
    options:
      members:
        - RedisCacheAdapter

::: grelmicro.cache.postgres
    options:
      members:
        - PostgresCacheAdapter
