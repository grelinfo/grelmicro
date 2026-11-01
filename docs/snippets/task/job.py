from dataclasses import dataclass
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException

from grelmicro import Grelmicro
from grelmicro.task import JobError, JobInfo, Progress, Tasks

tasks = Tasks()
micro = Grelmicro(uses=[tasks])


@tasks.job(max_concurrent=2)
async def import_rows(progress: Progress, upload_id: str) -> None:
    rows = await load_rows(upload_id)
    progress.total = len(rows)
    for row in rows:
        progress.checkpoint()
        if not row.valid:
            raise JobError(f"Row {row.number} is invalid")
        progress.advance(result=await save_row(row))


app = FastAPI()
micro.install(app)

UserId = Annotated[str, Header()]


@app.post("/imports", status_code=202)
async def start_import(upload_id: str, user_id: UserId) -> JobInfo:
    return await import_rows.for_owner(user_id).submit(upload_id)


@app.get("/imports/{job_id}")
async def read_import(job_id: str, user_id: UserId) -> JobInfo:
    job = await import_rows.for_owner(user_id).get(job_id)
    if job is None:
        raise HTTPException(404)
    return job


@app.post("/imports/{job_id}/cancel")
async def cancel_import(job_id: str, user_id: UserId) -> JobInfo:
    job = await import_rows.for_owner(user_id).cancel(job_id)
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


async def save_row(row: Row) -> str:
    return f"row-{row.number}"
