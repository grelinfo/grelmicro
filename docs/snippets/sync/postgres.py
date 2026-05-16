from grelmicro import Grelmicro
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.sync import Sync

postgres = PostgresProvider("postgresql://user:password@localhost:5432/db")
micro = Grelmicro(uses=[postgres, Sync(postgres)])
