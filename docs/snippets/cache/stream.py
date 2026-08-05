from collections.abc import AsyncIterator

from grelmicro import Grelmicro
from grelmicro.cache import JsonSerializer, TTLCache, cached
from grelmicro.providers.memory import MemoryProvider

micro = Grelmicro(uses=[MemoryProvider()])

cache = TTLCache(ttl=300, serializer=JsonSerializer())


@cached(cache, key="answer:{question_id}")
async def answer(question_id: int) -> AsyncIterator[str]:
    for token in ("The", " answer", " is", " 42."):
        yield token


async def main() -> None:
    async with micro:
        # Streams live from the producer, stores the tokens at the end.
        async for token in answer(1):
            print(token)
        # Replays the stored tokens, the producer does not run again.
        async for token in answer(1):
            print(token)
        # The same entry, read whole.
        tokens = await answer.collect(1)
        print("".join(tokens))
