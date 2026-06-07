from grelmicro import Grelmicro
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.resilience import CircuitBreaker, CircuitBreakers

sqlite = SQLiteProvider("app.db")
micro = Grelmicro(uses=[sqlite, CircuitBreakers(sqlite)])

payments = CircuitBreaker("payments")
