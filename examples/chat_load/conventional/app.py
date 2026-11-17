import asyncio
import os
import secrets
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query, WebSocket
from fastapi.responses import PlainTextResponse
from fastapi.websockets import WebSocketDisconnect
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from examples.chat_load.conventional.worker import CHECKSUM, queue
from examples.chat_load.faults import FaultKind, FaultRequest, RoomFault, trigger
from examples.chat_load.origin import require_same_origin
from examples.chat_load.protocol import (
    ROOM_PATTERN,
    Ack,
    Fault,
    JobAccepted,
    JobRequest,
    Message,
    RoomStats,
    Send,
)
from examples.chat_load.rooms import Hub

type Room = Annotated[str, Path(pattern=ROOM_PATTERN)]
type Client = Annotated[str, Query(pattern=ROOM_PATTERN)]


def topic(room: str) -> str:
    return "chat:" + room


async def messages(pubsub: PubSub) -> AsyncIterator[bytes]:
    async for item in pubsub.listen():
        data = item["data"]
        if item["type"] == "message" and isinstance(data, bytes):
            yield data


@dataclass(slots=True)
class Owner:
    kv: Redis | None = None
    hub: Hub | None = None


owner = Owner()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    kv = Redis.from_url(os.environ.get("KV_URL", "redis://kv:6379/0"))

    @asynccontextmanager
    async def channel(name: str) -> AsyncGenerator[AsyncIterable[bytes]]:
        async with kv.pubsub() as pubsub:
            await pubsub.subscribe(name)
            yield messages(pubsub)

    async with kv, asyncio.TaskGroup() as tasks:
        owner.kv, owner.hub = kv, Hub(channel, tasks)
        try:
            yield
        finally:
            owner.kv, owner.hub = None, None


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


async def store() -> Redis:
    if owner.kv is None:
        raise HTTPException(503, "KV cache is not connected")
    return owner.kv


async def rooms() -> Hub:
    if owner.hub is None:
        raise HTTPException(503, "Room hub is not running")
    return owner.hub


type Store = Annotated[Redis, Depends(store)]
type Rooms = Annotated[Hub, Depends(rooms)]


@app.get("/health", response_class=PlainTextResponse)
async def health(kv: Store) -> str:
    await kv.get("chat:health")
    return "ok"


@app.get("/rooms/{room}/stats")
async def stats(kv: Store, room: Room) -> RoomStats:
    messages, jobs = await kv.mget("chat:" + room, "chat-jobs:" + room)
    return RoomStats(messages=int(messages or 0), jobs=int(jobs or 0))


@app.post("/jobs", status_code=202)
async def submit(request: JobRequest) -> JobAccepted:
    job = secrets.token_hex(8)
    await asyncio.to_thread(queue.send_task, CHECKSUM, (job, request.room, request.size))
    return JobAccepted(job=job)


@app.post("/rooms/{room}/faults/{kind}", dependencies=[Depends(require_same_origin)])
async def fault(
    kv: Store,
    room: Room,
    kind: FaultKind,
    seconds: Annotated[float, Query(ge=0.1, le=5)] = 3,
) -> Fault:
    try:
        trigger(FaultRequest(kind, seconds))
    except RoomFault:
        notice = Fault(kind=kind, outcome="failed")
        await kv.publish(topic(room), notice.model_dump_json())
        raise
    notice = Fault(kind=kind, outcome="completed")
    await kv.publish(topic(room), notice.model_dump_json())
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
    websocket: WebSocket, kv: Redis, room: str, client: str, lock: asyncio.Lock
) -> None:
    while True:
        try:
            send = Send.model_validate_json(await websocket.receive_text())
        except ValidationError:
            await websocket.close(1003)
            return
        count = await kv.incr("chat:" + room)
        message = Message(sender=client, id=send.id, text=send.text, count=count)
        await kv.publish(topic(room), message.model_dump_json())
        async with lock:
            await websocket.send_text(Ack(id=send.id, count=count).model_dump_json())


@app.websocket("/rooms/{room}/ws")
async def chat(websocket: WebSocket, kv: Store, hub: Rooms, room: Room, client: Client) -> None:
    lock = asyncio.Lock()
    async with hub.join(topic(room)) as pushes:
        await websocket.accept()
        async with asyncio.TaskGroup() as tasks:
            forwarding = tasks.create_task(forward(websocket, pushes, lock))
            try:
                await receive(websocket, kv, room, client, lock)
            except WebSocketDisconnect:
                pass
            finally:
                forwarding.cancel()
