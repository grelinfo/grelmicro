from grelmicro import Grelmicro
from grelmicro.log import AccessLog, Log

micro = Grelmicro(
    uses=[
        Log(),
        AccessLog(
            exclude=("/internal/*",),
            quiet=("/livez", "/readyz", "/healthz", "/metrics", "/ping"),
        ),
    ]
)
