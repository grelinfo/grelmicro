import httpx

from grelmicro.resilience import shield


class MyRpcTimeout(Exception): ...  # noqa: N818


class MyLLMError(Exception): ...


@shield.internal(when=MyRpcTimeout)
async def call_internal_rpc() -> None: ...


@shield.api(when=(httpx.TimeoutException, httpx.ConnectError))
async def call_external_api() -> None: ...


@shield.slow(when=MyLLMError)
async def call_llm(prompt: str) -> str:
    return prompt
