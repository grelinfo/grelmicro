import httpx

from grelmicro.resilience import ApiShieldConfig, Shield

config = ApiShieldConfig(
    timeout_errors=(httpx.TimeoutException, httpx.ConnectError),
    max_rate=20.0,
)
github = Shield("github", config=config)
