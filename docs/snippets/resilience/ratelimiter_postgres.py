from grelmicro import Grelmicro
from grelmicro.providers.postgres import PostgresProvider

postgres = PostgresProvider("postgresql://localhost:5432/app")
micro = Grelmicro(uses=[postgres])
