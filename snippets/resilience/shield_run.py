import httpx

from grelmicro.resilience import Shield

github = Shield.api("github", timeout_errors=(httpx.TimeoutException,))


async def parse_response(response: httpx.Response) -> bytes:
    return response.content


async def handler(client: httpx.AsyncClient, url: str) -> bytes:
    response = await github.run(client.get, url)
    return await github.run(parse_response, response)
