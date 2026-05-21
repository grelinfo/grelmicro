from grelmicro.resilience import Timeout, TimeoutConfig

config = TimeoutConfig(seconds=2.0)
db_timeout = Timeout("db", config=config)
