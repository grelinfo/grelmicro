from grelmicro import Grelmicro
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.resilience import CircuitBreaker, CircuitBreakerRegistry

sqlite = SQLiteProvider("app.db")
micro = Grelmicro(uses=[CircuitBreakerRegistry(sqlite)])

payments = CircuitBreaker("payments")
