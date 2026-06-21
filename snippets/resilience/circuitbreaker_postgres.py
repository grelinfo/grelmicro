from grelmicro import Grelmicro
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.resilience import CircuitBreaker, CircuitBreakers

postgres = PostgresProvider("postgresql://localhost:5432/app")
micro = Grelmicro(uses=[CircuitBreakers(postgres)])

payments = CircuitBreaker("payments")
