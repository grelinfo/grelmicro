from grelmicro import Grelmicro
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.resilience import RateLimiterRegistry

sqlite = SQLiteProvider("rate_limit.db")
micro = Grelmicro(uses=[sqlite, RateLimiterRegistry(sqlite)])
