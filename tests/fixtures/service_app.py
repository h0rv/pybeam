import asyncio
from base64 import b64encode
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request, Response, WebSocket
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.websockets import WebSocketDisconnect

from pybeam.integrations.fastapi.dependencies import BeamProvider
from pybeam.integrations.hornbeam import Services


@dataclass
class Diagnostics:
    ticks: int = 0
    active_floods: int = 0
    completed_floods: int = 0
    output_disconnects: int = 0
    sent_flood_frames: int = 0


diagnostics = Diagnostics()
provider = BeamProvider()
BeamDependency = Annotated[Services, Depends(provider)]


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    stop = asyncio.Event()

    async def tick() -> None:
        while not stop.is_set():
            diagnostics.ticks += 1
            await asyncio.sleep(0.02)

    async with provider.lifespan(_app), asyncio.TaskGroup() as group:
        task = group.create_task(tick())
        try:
            yield
        finally:
            stop.set()
            task.cancel()
    print("LIFESPAN_STOPPED", flush=True)


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health(beam: BeamDependency) -> dict[str, bool]:
    return {"ok": True}


@app.get("/ticks")
async def ticks(beam: BeamDependency) -> dict[str, int]:
    return {"ticks": diagnostics.ticks}


@app.get("/counter")
async def counter(beam: BeamDependency) -> dict[str, int]:
    return {"count": await beam.incr("counter", 0, namespace="runtime-tests")}


@app.post("/counter")
async def increment(beam: BeamDependency) -> dict[str, int]:
    return {"count": await beam.incr("counter", namespace="runtime-tests")}


@app.put("/cache/{namespace}/{key}")
async def put_cache(
    namespace: str, key: str, request: Request, beam: BeamDependency, ttl: float | None = None
) -> Response:
    await beam.store(namespace).set(key, await request.body(), expires_in=ttl)
    return Response(status_code=204)


@app.get("/cache/{namespace}/{key}")
async def get_cache(
    namespace: str, key: str, beam: BeamDependency, renew: float | None = None
) -> Response:
    value = await beam.store(namespace).get(key, renew_for=renew)
    if value is None:
        return Response(status_code=404)
    return Response(value, media_type="application/octet-stream")


@app.get("/subscriptions")
async def subscriptions(beam: BeamDependency) -> dict[str, int]:
    return {"count": await beam.subscription_count()}


@app.post("/publish/{topic}")
async def publish(topic: str, request: Request, beam: BeamDependency) -> dict[str, int]:
    return {"targeted": await beam.publish(topic, await request.body())}


@app.get("/events/{topic}", response_class=EventSourceResponse)
async def events(topic: str, beam: BeamDependency) -> AsyncGenerator[ServerSentEvent]:
    async with beam.subscribe(topic) as subscription:
        yield ServerSentEvent(comment="subscribed")
        async for payload in subscription:
            yield ServerSentEvent(event="message", raw_data=b64encode(payload).decode("ascii"))


class OutputState(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    active: int
    completed: int
    disconnected: int
    sent: int


@app.get("/output-state")
async def output_state(beam: BeamDependency) -> OutputState:
    return OutputState(
        active=diagnostics.active_floods,
        completed=diagnostics.completed_floods,
        disconnected=diagnostics.output_disconnects,
        sent=diagnostics.sent_flood_frames,
    )


@app.websocket("/slow-output")
async def slow_output(socket: WebSocket, beam: BeamDependency) -> None:
    diagnostics.active_floods += 1
    try:
        await socket.accept()
        async with beam.subscribe("runtime-tests:slow-output"):
            payload = b"x" * (8 * 1024 * 1024)
            try:
                while True:
                    await socket.send_bytes(payload)
                    diagnostics.sent_flood_frames += 1
            except WebSocketDisconnect:
                diagnostics.output_disconnects += 1
    finally:
        diagnostics.active_floods -= 1
        diagnostics.completed_floods += 1


@app.websocket("/echo")
async def echo(socket: WebSocket, beam: BeamDependency) -> None:
    await socket.accept()
    while True:
        text = await socket.receive_text()
        await socket.send_text(text)


@app.websocket("/idle")
async def idle(socket: WebSocket, beam: BeamDependency) -> None:
    await socket.accept()
    await asyncio.sleep(0.25)
    await socket.send_text("idle-push")
    await socket.close()


class Command(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    action: Literal["increment", "snapshot", "cancel"]
    amount: Annotated[int, Field(ge=1, le=10)] = 1


class Snapshot(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    count: int


class StopConnection(Exception):
    pass


@app.get("/shared-state")
async def shared_state(beam: BeamDependency) -> Snapshot:
    return Snapshot(count=await beam.incr("shared", 0, namespace="vertical"))


@app.post("/shared-increment")
async def shared_increment(beam: BeamDependency) -> Snapshot:
    count = await beam.incr("shared", namespace="vertical")
    await beam.publish("vertical:state", b"invalidate")
    return Snapshot(count=count)


@app.websocket("/shared")
async def shared(socket: WebSocket, beam: BeamDependency) -> None:
    slow = socket.query_params.get("slow") == "1"
    outbound: asyncio.Queue[Snapshot] = asyncio.Queue(maxsize=2)

    async def snapshot() -> Snapshot:
        return Snapshot(count=await beam.incr("shared", 0, namespace="vertical"))

    async def writer() -> None:
        previous = -1
        while True:
            state = await outbound.get()
            if state.count >= previous:
                await socket.send_text(state.model_dump_json())
                previous = state.count

    async def requests() -> None:
        try:
            while True:
                command = Command.model_validate_json(await socket.receive_text())
                match command.action:
                    case "increment":
                        await beam.incr("shared", command.amount, namespace="vertical")
                        await beam.publish("vertical:state", b"invalidate")
                    case "snapshot":
                        await outbound.put(await snapshot())
                    case "cancel":
                        raise StopConnection
        except (WebSocketDisconnect, ValidationError) as exc:
            raise StopConnection from exc

    await socket.accept()
    try:
        async with beam.subscribe("vertical:state", max_bytes=1024 if slow else 65536) as updates:
            await outbound.put(await snapshot())

            async def notifications() -> None:
                async for _payload in updates:
                    if slow:
                        await asyncio.sleep(0.25)
                    await outbound.put(await snapshot())
                raise StopConnection

            async with asyncio.TaskGroup() as group:
                group.create_task(writer())
                group.create_task(requests())
                group.create_task(notifications())
    except* StopConnection:
        pass
