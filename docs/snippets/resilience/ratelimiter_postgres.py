from grelmicro import Grelmicro
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.resilience import RateLimiters

postgres = PostgresProvider("postgresql://localhost:5432/app")
micro = Grelmicro(uses=[RateLimiters(postgres)])
