from grelmicro import Grelmicro
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.resilience import CircuitBreaker

sqlite = SQLiteProvider("app.db")
micro = Grelmicro(uses=[sqlite])

payments = CircuitBreaker("payments")
