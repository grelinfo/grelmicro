from grelmicro import Grelmicro
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.resilience import CircuitBreaker, CircuitBreakerRegistry

postgres = PostgresProvider("postgresql://localhost:5432/app")
micro = Grelmicro(uses=[CircuitBreakerRegistry(postgres)])

payments = CircuitBreaker("payments")
