import httpx

from grelmicro.resilience import ApiShieldConfig, Shield

config = ApiShieldConfig(
    when=(httpx.TimeoutException, httpx.ConnectError),
    max_rate=20.0,
)
github = Shield.from_config("github", config)
