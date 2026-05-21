from grelmicro.resilience import Timeout

# Reads the deadline from environment variables.
#
# - GREL_TIMEOUT_DB_SECONDS=2.0
db_timeout = Timeout("db")
