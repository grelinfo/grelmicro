# Cache

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
