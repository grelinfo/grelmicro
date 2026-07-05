import os

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.providers.postgres import PostgresProvider

postgres = PostgresProvider(os.environ["POSTGRES_URL"])
micro = Grelmicro(uses=[Coordination(postgres)])
