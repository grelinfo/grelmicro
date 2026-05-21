from grelmicro.resilience import Timeout

db_timeout = Timeout("db", seconds=2.0)


@db_timeout
async def fetch_rows(db) -> list[dict]:
    return await db.fetch_all("SELECT * FROM accounts")
