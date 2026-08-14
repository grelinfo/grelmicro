from pydantic import BaseModel

from grelmicro.resilience import Timeout

db_timeout = Timeout("db", seconds=2.0)


class Account(BaseModel):
    id: int


async def fetch_rows(db) -> list[Account]:
    async with db_timeout:
        return await db.fetch_all("SELECT * FROM accounts")
