from dataclasses import dataclass

from fastapi import FastAPI, HTTPException

from grelmicro import Grelmicro
from grelmicro.http import AuthenticatedRequests
from grelmicro.integrations.fastapi import CurrentPrincipal
from grelmicro.providers.redis import RedisProvider
from grelmicro.security import JWTVerifier
from grelmicro.task import JobError, JobInfo, Progress, Tasks

tasks = Tasks()
micro = Grelmicro(
    uses=[
        RedisProvider(),
        tasks,
        AuthenticatedRequests(
            JWTVerifier.discover(
                "https://auth.example.com/", audience="imports-api"
            ),
        ),
    ]
)


@tasks.job(max_concurrent=2)
async def import_rows(progress: Progress, upload_id: str) -> dict[str, int]:
    rows = await load_rows(upload_id)
    progress.total = len(rows)
    for row in rows:
        progress.checkpoint()
        if not row.valid:
            raise JobError(f"Row {row.number} is invalid")
        await save_row(row)
        progress.advance()
    return {"imported": len(rows)}


app = FastAPI()
micro.install(app)


@app.post("/imports", status_code=202)
async def start_import(upload_id: str, caller: CurrentPrincipal) -> JobInfo:
    return await import_rows.for_owner(caller).submit(upload_id)


@app.get("/imports/{job_id}")
async def read_import(job_id: str, caller: CurrentPrincipal) -> JobInfo:
    job = await import_rows.for_owner(caller).get(job_id)
    if job is None:
        raise HTTPException(404)
    return job


@app.post("/imports/{job_id}/cancel")
async def cancel_import(job_id: str, caller: CurrentPrincipal) -> JobInfo:
    job = await import_rows.for_owner(caller).cancel(job_id)
    if job is None:
        raise HTTPException(404)
    return job


# Stand-ins for your own code.
@dataclass
class Row:
    number: int
    valid: bool


async def load_rows(upload_id: str) -> list[Row]:
    return [Row(number, valid=True) for number in range(1, 4)]


async def save_row(row: Row) -> None:
    print(f"Saved row {row.number}")
