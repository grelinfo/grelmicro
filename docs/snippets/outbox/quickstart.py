import asyncio
import os

from pydantic import BaseModel

from grelmicro import Grelmicro
from grelmicro.outbox import Message, Outbox
from grelmicro.providers.postgres import PostgresProvider

postgres = PostgresProvider(os.environ["POSTGRES_URL"])
outbox = Outbox(postgres)

micro = Grelmicro(uses=[outbox])
delivered = asyncio.Event()


class WelcomeEmail(BaseModel):
    to: str
    user_id: int


async def send_email(to: str, idempotency_key: str) -> None:
    print(f"welcome email to {to}")
    delivered.set()


@outbox.handler(WelcomeEmail)
async def send_welcome(message: Message[WelcomeEmail]) -> None:
    assert message.data is not None  # a typed handler always gets its model
    await send_email(to=message.data.to, idempotency_key=str(message.id))


async def sign_up(email: str) -> None:
    async with postgres.client.acquire() as conn, conn.transaction():
        user_id = await conn.fetchval(
            "INSERT INTO users (email) VALUES ($1) RETURNING id", email
        )
        await outbox.publish(conn, WelcomeEmail(to=email, user_id=user_id))


async def main() -> None:
    async with micro:
        await postgres.client.execute(
            "CREATE TABLE IF NOT EXISTS users (id serial PRIMARY KEY, email text)"
        )
        await sign_up("alice@example.com")
        async with asyncio.timeout(5):
            await delivered.wait()


if __name__ == "__main__":
    asyncio.run(main())
