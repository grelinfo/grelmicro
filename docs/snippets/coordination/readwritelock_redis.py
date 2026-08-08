from grelmicro import Grelmicro
from grelmicro.coordination import Coordination, ReadWriteLock
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[Coordination(redis)])

catalog = ReadWriteLock("catalog")
