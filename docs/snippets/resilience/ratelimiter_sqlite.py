from grelmicro import Grelmicro
from grelmicro.providers.sqlite import SQLiteProvider

sqlite = SQLiteProvider("rate_limit.db")
micro = Grelmicro(uses=[sqlite])
