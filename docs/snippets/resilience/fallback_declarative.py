import httpx

from grelmicro.resilience import Fallback, FallbackConfig, Match

config = FallbackConfig(
    when=Match.exception(httpx.HTTPError),
    default=[],
)
policy = Fallback.from_config("recs", config)
