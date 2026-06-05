from grelmicro import Grelmicro
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.resilience import RateLimiters

sqlite = SQLiteProvider("rate_limit.db")
micro = Grelmicro(uses=[sqlite, RateLimiters(sqlite)])
