from grelmicro.resilience import shield


class APIConnectionError(Exception): ...


@shield.api(
    "stripe",
    timeout_errors=(APIConnectionError,),
    max_rate=10.0,
)
async def charge_card(amount: int) -> None:
    return None
