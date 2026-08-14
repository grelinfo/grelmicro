from pydantic import BaseModel

from grelmicro.resilience import Timeout

db_timeout = Timeout("db", seconds=2.0)


class Account(BaseModel):
    id: int


@db_timeout
async def fetch_rows(db) -> list[Account]:
    return await db.fetch_all("SELECT * FROM accounts")
