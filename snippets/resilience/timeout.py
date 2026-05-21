from grelmicro.resilience import Timeout

db_timeout = Timeout("db", seconds=2.0)


async def fetch_rows(db) -> list[dict]:
    async with db_timeout:
        return await db.fetch_all("SELECT * FROM accounts")
