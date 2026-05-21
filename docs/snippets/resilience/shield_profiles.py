import httpx

from grelmicro.resilience import shield


class MyRpcTimeout(Exception): ...  # noqa: N818


class MyLLMError(Exception): ...


@shield.internal(timeout_errors=(MyRpcTimeout,))
async def call_internal_rpc() -> None: ...


@shield.api(timeout_errors=(httpx.TimeoutException, httpx.ConnectError))
async def call_external_api() -> None: ...


@shield.slow(timeout_errors=(MyLLMError,))
async def call_llm(prompt: str) -> str:
    return prompt
