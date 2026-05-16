from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider
from grelmicro.sync import Sync

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis, Sync(redis)])
