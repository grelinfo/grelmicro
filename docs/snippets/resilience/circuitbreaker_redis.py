from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import Breaker, CircuitBreaker

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis, Breaker(redis)])

payments = CircuitBreaker("payments")
