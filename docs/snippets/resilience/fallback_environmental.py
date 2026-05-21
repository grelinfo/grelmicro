from grelmicro.resilience import Fallback

# Reads the config from environment variables. `factory` cannot be set
# from env (it is a callable), use `default` or build the config from code.
#
# - GREL_FALLBACK_RECS_WHEN=httpx.HTTPError
# - GREL_FALLBACK_RECS_DEFAULT=[]
policy = Fallback("recs")
