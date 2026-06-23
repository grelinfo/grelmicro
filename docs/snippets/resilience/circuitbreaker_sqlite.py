from grelmicro import Grelmicro
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.resilience import CircuitBreaker, CircuitBreakerRegistry

sqlite = SQLiteProvider("app.db")
micro = Grelmicro(uses=[sqlite, CircuitBreakerRegistry(sqlite)])

payments = CircuitBreaker("payments")
