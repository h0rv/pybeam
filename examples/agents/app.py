import asyncio
import os
import signal
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles

from examples.agents.protocol import TERMINAL, Announcement, Node, Scenario, SessionView, Snapshot
from examples.agents.runtime import ANNOUNCEMENTS, Runtime, stop_topic, topic
from examples.agents.session import Session
from examples.agents.tools import executions
from examples.agents.views import templates, transcript
from examples.chat_load.origin import require_same_origin
from pybeam import ActorGroup
from pybeam.integrations.hornbeam import Beam


@dataclass(slots=True)
class Owner:
    runtime: Runtime | None = None


owner = Owner()


def node_alias(value: str) -> Node:
    if value not in ("a", "b", "c"):
        raise ValueError("Expected node a, b or c")
    return value


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    node = node_alias(os.environ["PYBEAM_AGENTS_NODE"])
    replicas = ["agents_" + name + "@localhost" for name in "abc"]
    beam = await Beam.connect()
    async with asyncio.TaskGroup() as tasks:
        runtime = Runtime(
            beam, node, replicas, Path(os.environ["PYBEAM_AGENTS_AUDIT"]), tasks, transcript
        )
        announcements_ready, updates_ready = asyncio.Event(), asyncio.Event()
        announcements = tasks.create_task(runtime.announcements(announcements_ready))
        updates = tasks.create_task(runtime.updates(updates_ready))
        await announcements_ready.wait()
        await updates_ready.wait()
        async with ActorGroup(replicas=replicas) as warm:
            await warm.restore("warm-" + node, Session)
        owner.runtime = runtime
        try:
            yield
        finally:
            owner.runtime = None
            announcements.cancel()
            updates.cancel()
            for placement in runtime.placements.values():
                placement.task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parents[1] / "static"), name="static")
app.mount("/assets", StaticFiles(directory=Path(__file__).parent / "static"), name="assets")


def runtime() -> Runtime:
    if owner.runtime is None:
        raise HTTPException(503, "Agents are starting")
    return owner.runtime


type Running = Annotated[Runtime, Depends(runtime)]


def session_snapshot(current: Runtime, session: UUID) -> Snapshot:
    value = current.snapshots.get(session.hex)
    if value is None:
        raise HTTPException(404, "Unknown session")
    return value


@app.get("/health")
async def health(current: Running) -> dict[str, str]:
    return {"node": current.node, "status": "ready"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, current: Running) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"node": current.node, "sessions": list(current.snapshots.values())},
    )


@app.post("/sessions", dependencies=[Depends(require_same_origin)])
async def start(
    current: Running,
    mode: Annotated[Scenario, Form()] = "normal",
    count: Annotated[int, Form(ge=1, le=12)] = 1,
    placement: Annotated[Node, Form()] = "a",
) -> RedirectResponse:
    if len(current.snapshots) + count > 100:
        raise HTTPException(503, "Demo session limit reached; restart the launcher for a fresh run")
    if sum(value.state not in TERMINAL for value in current.snapshots.values()) + count > 24:
        raise HTTPException(503, "At most 24 active sessions in this demo")
    for _ in range(count):
        announcement = Announcement(id=uuid4().hex, prompt=mode, owner=placement)
        await current.beam.publish(ANNOUNCEMENTS, announcement.model_dump_json().encode())
        await current.wait(announcement.id)
    return RedirectResponse("/", status_code=303)


@app.get("/api/sessions")
async def sessions(current: Running) -> list[Snapshot]:
    return list(current.snapshots.values())


@app.get("/api/sessions/{session}")
async def inspect_session(session: UUID, current: Running) -> SessionView:
    snapshot = session_snapshot(current, session)
    audit = await asyncio.to_thread(executions, current.audit, snapshot.id)
    return SessionView(snapshot=snapshot, executions=audit)


@app.get("/sessions/{session}", response_class=HTMLResponse)
async def detail(request: Request, session: UUID, current: Running) -> HTMLResponse:
    snapshot = session_snapshot(current, session)
    return templates.TemplateResponse(
        request=request,
        name="session.html",
        context={"snapshot": snapshot, "node": current.node},
    )


@app.post("/sessions/{session}/stop", dependencies=[Depends(require_same_origin)])
async def stop(session: UUID, current: Running) -> RedirectResponse:
    snapshot = session_snapshot(current, session)
    if snapshot.state not in TERMINAL:
        try:
            async with asyncio.timeout(10):
                while True:
                    await current.beam.publish(stop_topic(session.hex), b"stop")
                    try:
                        async with asyncio.timeout(0.2):
                            await current.wait(session.hex, terminal=True)
                            break
                    except TimeoutError:
                        continue
        except TimeoutError as error:
            raise HTTPException(503, "Stop acknowledgement timed out; outcome uncertain") from error
    return RedirectResponse("/sessions/" + session.hex, status_code=303)


@app.get("/sessions/{session}/events", response_class=EventSourceResponse)
async def events(
    session: UUID, request: Request, current: Running
) -> AsyncGenerator[ServerSentEvent]:
    session_snapshot(current, session)
    scoped = current.beam.for_scope(request.scope)
    async with scoped.subscribe(topic(session.hex)) as subscription:
        yield ServerSentEvent(raw_data=transcript(session_snapshot(current, session)))
        async for fragment in subscription:
            yield ServerSentEvent(raw_data=fragment.decode())


@app.post("/node/kill", dependencies=[Depends(require_same_origin)])
async def kill_node() -> None:
    os.kill(os.getpid(), signal.SIGKILL)
