# Cache

- **Start here**: [Cache guide](../cache.md)
- **Common recipes**: [`@cached`](../cache.md#cached-decorator), [`TTLCache`](../cache.md#ttlcache)
- **Configuration**: [Backend setup](../cache.md#backend), [Redis backend configuration](../cache.md#redis-backend-configuration)

::: grelmicro.cache
    options:
      show_submodules: true
      members:
        - CacheBackend
        - CacheError
        - CacheInfo
        - CacheSerializer
        - CacheSettingsValidationError
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
