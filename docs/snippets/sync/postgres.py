from grelmicro.providers.postgres import PostgresProvider
from grelmicro.sync.postgres import PostgresSyncAdapter

provider = PostgresProvider("postgresql://user:password@localhost:5432/db")
backend = PostgresSyncAdapter(provider=provider)
