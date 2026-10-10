import httpx

from grelmicro.resilience import Shield

github = Shield.api(
    "github",
    when=(httpx.TimeoutException, httpx.ConnectError),
    max_rate=20.0,
)
