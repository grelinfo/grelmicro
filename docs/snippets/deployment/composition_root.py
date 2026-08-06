from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider

redis = RedisProvider()
micro = Grelmicro(uses=[redis])
