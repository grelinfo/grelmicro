from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import CircuitBreaker

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[redis])

payments = CircuitBreaker("payments")
