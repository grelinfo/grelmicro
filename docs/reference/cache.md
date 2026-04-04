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
        - MemoryCacheBackend

::: grelmicro.cache.redis
    options:
      members:
        - RedisCacheBackend
