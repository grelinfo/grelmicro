from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.providers.postgres import PostgresProvider

postgres = PostgresProvider("postgresql://user:password@localhost:5432/db")
micro = Grelmicro(uses=[Coordination(postgres)])
