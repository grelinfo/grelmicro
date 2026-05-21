from grelmicro.resilience import Timeout

db_timeout = Timeout("db", seconds=2.0)
