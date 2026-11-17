import argparse
import asyncio
import logging
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, WebSocket
from fastapi.responses import PlainTextResponse
from fastapi.websockets import WebSocketDisconnect
from pydantic import ValidationError
from uvicorn import Config, Server

from examples.chat_load.faults import FaultKind, FaultRequest, RoomFault, abort_entry, worker_fault
from examples.chat_load.jobs import checksum_entry
from examples.chat_load.protocol import (
    ROOM_PATTERN,
    Ack,
    Fault,
    JobAccepted,
    JobDone,
    JobRequest,
    Message,
    RoomStats,
    Send,
)
from examples.chat_load.rooms import Hub
from pybeam.codec import Json
from pybeam.integrations.hornbeam import Beam, Services, run
from pybeam.submissions import Overloaded
from pybeam.tasks import (
    ExecutionMode,
    TaskCrashed,
    TaskFunction,
    TaskLimits,
    TaskRef,
    TaskSupervisor,
)

CHECKSUM = TaskFunction(checksum_entry, Json(int), Json(int), dedicated_loop=True)
WORKER_FAULT = TaskFunction(worker_fault, Json(FaultRequest), Json(None), dedicated_loop=True)
ABORT = TaskFunction(abort_entry, Json(None), Json(None), execution=ExecutionMode.ISOLATED)

type Room = Annotated[str, Path(pattern=ROOM_PATTERN)]
type Client = Annotated[str, Query(pattern=ROOM_PATTERN)]


def topic(room: str) -> str:
    return "chat:" + room


@dataclass(slots=True)
class Jobs:
    beam: Services
    supervisor: TaskSupervisor
    tasks: asyncio.TaskGroup
    running: set[str] = field(default_factory=set)

    async def submit(self, request: JobRequest) -> str:
        ref = await self.supervisor.start(CHECKSUM, request.size)
        job = secrets.token_hex(8)
        self.running.add(job)
        self.tasks.create_task(self.finish(job, request.room, ref))
        return job

    async def finish(self, job: str, room: str, ref: TaskRef[int]) -> None:
        try:
            result: int | None = await ref
        except TaskCrashed:
            result = None
        finally:
            self.running.discard(job)
        await self.beam.incr(room, namespace="chat-jobs")
        done = JobDone(job=job, result=result)
        await self.beam.publish(topic(room), done.model_dump_json().encode())


@dataclass(slots=True)
class Owner:
    beam: Services | None = None
    jobs: Jobs | None = None
    hub: Hub | None = None


owner = Owner()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    limits = TaskLimits(capacity=app.state.job_slots, max_bytes=65_536, shutdown_ms=5_000)
    beam = await Beam.connect()
    async with TaskSupervisor(limits) as supervisor, asyncio.TaskGroup() as tasks:
        owner.beam, owner.jobs = beam, Jobs(beam, supervisor, tasks)
        owner.hub = Hub(beam.subscribe, tasks)
        try:
            yield
        finally:
            owner.beam, owner.jobs, owner.hub = None, None, None


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.state.job_slots = 64


async def services() -> Services:
    if owner.beam is None:
        raise HTTPException(503, "PyBEAM services are not running")
    return owner.beam


async def running_jobs() -> Jobs:
    if owner.jobs is None:
        raise HTTPException(503, "Job runner is not running")
    return owner.jobs


async def rooms() -> Hub:
    if owner.hub is None:
        raise HTTPException(503, "Room hub is not running")
    return owner.hub


type Shared = Annotated[Services, Depends(services)]
type Rooms = Annotated[Hub, Depends(rooms)]
type Runner = Annotated[Jobs, Depends(running_jobs)]


@app.get("/health", response_class=PlainTextResponse)
async def health(beam: Shared) -> str:
    await beam.incr("health", 0, namespace="chat")
    return "ok"


@app.get("/rooms/{room}/stats")
async def stats(beam: Shared, room: Room) -> RoomStats:
    return RoomStats(
        messages=await beam.incr(room, 0, namespace="chat"),
        jobs=await beam.incr(room, 0, namespace="chat-jobs"),
    )


@app.post("/jobs", status_code=202)
async def submit(jobs: Runner, request: JobRequest) -> JobAccepted:
    try:
        return JobAccepted(job=await jobs.submit(request))
    except Overloaded as error:
        raise HTTPException(503, "Job slots are full") from error


@app.post("/rooms/{room}/faults/{kind}")
async def fault(
    jobs: Runner,
    beam: Shared,
    room: Room,
    kind: FaultKind,
    seconds: Annotated[float, Query(ge=0.1, le=5)] = 3,
) -> Fault:
    outcome: Literal["completed", "contained"] = "completed"
    try:
        if kind == "abort":
            ref = await jobs.supervisor.start(ABORT, None)
        else:
            ref = await jobs.supervisor.start(WORKER_FAULT, FaultRequest(kind, seconds))
        await ref
    except RoomFault, TaskCrashed:
        outcome = "contained"
    except Overloaded as error:
        raise HTTPException(503, "Job slots are full") from error
    notice = Fault(kind=kind, outcome=outcome)
    await beam.publish(topic(room), notice.model_dump_json().encode())
    return notice


async def forward(websocket: WebSocket, pushes: asyncio.Queue[bytes], lock: asyncio.Lock) -> None:
    while True:
        try:
            payload = await pushes.get()
        except asyncio.QueueShutDown:
            await websocket.close(1013)
            return
        async with lock:
            await websocket.send_text(payload.decode())


async def receive(
    websocket: WebSocket, beam: Services, room: str, client: str, lock: asyncio.Lock
) -> None:
    while True:
        try:
            send = Send.model_validate_json(await websocket.receive_text())
        except ValidationError:
            await websocket.close(1003)
            return
        count = await beam.incr(room, namespace="chat")
        message = Message(sender=client, id=send.id, text=send.text, count=count)
        await beam.publish(topic(room), message.model_dump_json().encode())
        async with lock:
            await websocket.send_text(Ack(id=send.id, count=count).model_dump_json())


@app.websocket("/rooms/{room}/ws")
async def chat(websocket: WebSocket, beam: Shared, hub: Rooms, room: Room, client: Client) -> None:
    lock = asyncio.Lock()
    async with hub.join(topic(room)) as pushes:
        await websocket.accept()
        async with asyncio.TaskGroup() as tasks:
            forwarding = tasks.create_task(forward(websocket, pushes, lock))
            try:
                await receive(websocket, beam, room, client, lock)
            except WebSocketDisconnect:
                pass
            finally:
                forwarding.cancel()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Chat rooms on one PyBEAM process")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--max-connections", type=int, default=4096)
    parser.add_argument("--job-slots", type=int, default=64)
    args = parser.parse_args()
    app.state.job_slots = args.job_slots
    logging.basicConfig(level=logging.WARNING)
    server = Server(
        Config(
            app,
            host="127.0.0.1",
            port=args.port,
            loop="none",
            http="h11",
            ws="websockets-sansio",
            limit_concurrency=args.max_connections,
            backlog=args.max_connections,
            lifespan="on",
            log_level="warning",
            timeout_graceful_shutdown=2,
        )
    )
    await server.serve()


if __name__ == "__main__":
    run(main)
