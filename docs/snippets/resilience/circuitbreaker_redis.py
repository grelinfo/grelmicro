from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import CircuitBreaker, CircuitBreakerRegistry

redis = RedisProvider("redis://localhost:6379/0")
micro = Grelmicro(uses=[CircuitBreakerRegistry(redis)])

payments = CircuitBreaker("payments")
