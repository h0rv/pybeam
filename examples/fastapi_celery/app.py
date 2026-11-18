import argparse
import asyncio
import secrets
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from uvicorn import Config, Server

from examples.chat_load.origin import require_same_origin
from pybeam.codec import Json
from pybeam.integrations.hornbeam import Beam, Services, run
from pybeam.submissions import Overloaded
from pybeam.tasks import TaskFunction, TaskLimits, TaskRef, TaskSupervisor


class TaskRequest(BaseModel):
    type: Literal[1, 2, 3]


class Accepted(BaseModel):
    task_id: str


class Status(BaseModel):
    task_id: str
    task_status: Literal["PENDING", "SUCCESS", "FAILURE"] = "PENDING"
    task_result: bool | str | None = None


async def sleep_task(seconds: float) -> bool:
    time.sleep(seconds)  # noqa: ASYNC251
    return True


SLEEP = TaskFunction(sleep_task, Json(float), Json(bool), dedicated_loop=True)
NAMESPACE = "fastapi-celery-results"
RESULT_TTL = 86_400


def topic(task_id: str) -> str:
    return "fastapi-celery:" + task_id


@dataclass(slots=True)
class Jobs:
    beam: Services
    supervisor: TaskSupervisor
    tasks: asyncio.TaskGroup
    unit: float

    async def save(self, status: Status) -> None:
        payload = status.model_dump_json().encode()
        await self.beam.store(NAMESPACE).set(status.task_id, payload, expires_in=RESULT_TTL)
        await self.beam.publish(topic(status.task_id), payload)

    async def read(self, task_id: str) -> Status:
        payload = await self.beam.store(NAMESPACE).get(task_id)
        return Status(task_id=task_id) if payload is None else Status.model_validate_json(payload)

    async def submit(self, request: TaskRequest) -> Accepted:
        ref = await self.supervisor.start(SLEEP, request.type * self.unit)
        task_id = secrets.token_hex(16)
        await self.save(Status(task_id=task_id))
        self.tasks.create_task(self.finish(task_id, ref))
        return Accepted(task_id=task_id)

    async def finish(self, task_id: str, ref: TaskRef[bool]) -> None:
        try:
            result = await ref
        except Exception:
            status = Status(task_id=task_id, task_status="FAILURE", task_result="Task crashed")
        else:
            status = Status(task_id=task_id, task_status="SUCCESS", task_result=result)
        await self.save(status)


@dataclass(slots=True)
class Owner:
    jobs: Jobs | None = None
    unit: float = 10


owner = Owner()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    beam = await Beam.connect()
    limits = TaskLimits(capacity=64, max_bytes=65_536, shutdown_ms=5_000)
    async with TaskSupervisor(limits) as supervisor, asyncio.TaskGroup() as tasks:
        owner.jobs = Jobs(beam, supervisor, tasks, owner.unit)
        try:
            yield
        finally:
            owner.jobs = None


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
app.mount("/static", StaticFiles(directory=Path(__file__).parent.parent / "static"), name="static")


async def running_jobs() -> Jobs:
    if owner.jobs is None:
        raise HTTPException(503, "Task runner is not running")
    return owner.jobs


type Runner = Annotated[Jobs, Depends(running_jobs)]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/tasks", status_code=201)
async def submit(request: TaskRequest, jobs: Runner) -> Accepted:
    try:
        return await jobs.submit(request)
    except Overloaded as error:
        raise HTTPException(503, "Task slots are full") from error


@app.get("/tasks/{task_id}")
async def status(task_id: str, jobs: Runner) -> Status:
    return await jobs.read(task_id)


@app.post("/rows", response_class=HTMLResponse, dependencies=[Depends(require_same_origin)])
async def row(
    request: Request, jobs: Runner, type: Annotated[Literal["1", "2", "3"], Form()]
) -> HTMLResponse:
    kinds: dict[str, Literal[1, 2, 3]] = {"1": 1, "2": 2, "3": 3}
    accepted = await submit(TaskRequest(type=kinds[type]), jobs)
    return templates.TemplateResponse(
        request=request, name="row.html", context={"status": Status(task_id=accepted.task_id)}
    )


async def events(task_id: str, jobs: Jobs) -> AsyncGenerator[ServerSentEvent]:
    async with jobs.beam.subscribe(topic(task_id)) as subscription:
        while True:
            current = await jobs.read(task_id)
            content = templates.get_template("cells.html").render(status=current)
            yield ServerSentEvent(raw_data=content)
            if current.task_status != "PENDING":
                yield ServerSentEvent(event="done", raw_data="")
                return
            await subscription.receive()


@app.get("/tasks/{task_id}/events", response_class=EventSourceResponse)
async def stream(task_id: str, jobs: Runner) -> AsyncGenerator[ServerSentEvent]:
    async with aclosing(events(task_id, jobs)) as updates:
        async for event in updates:
            yield event


async def main() -> None:
    parser = argparse.ArgumentParser(description="Background tasks with PyBEAM")
    parser.add_argument("--port", type=int, default=8810)
    parser.add_argument("--task-unit", type=float, default=10)
    args = parser.parse_args()
    if not 0 < args.task_unit <= 3600:
        parser.error("--task-unit must be positive and at most 3600 seconds")
    owner.unit = args.task_unit
    await Server(Config(app, host="127.0.0.1", port=args.port, loop="none", lifespan="on")).serve()


if __name__ == "__main__":
    run(main)
