from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import CircuitBreaker, CircuitBreakers

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis, CircuitBreakers(redis)])

payments = CircuitBreaker("payments")
