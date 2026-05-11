from grelmicro.providers.redis import RedisProvider
from grelmicro.sync.redis import RedisSyncAdapter

backend = RedisSyncAdapter(provider=RedisProvider("redis://localhost:6379/0"))
