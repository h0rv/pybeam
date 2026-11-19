import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import TypeAdapter
from redis.asyncio import Redis

from examples.agents.protocol import TERMINAL, Scenario
from examples.agents.usual import store
from examples.agents.usual.models import Turn, View
from examples.agents.usual.worker import TURN, queue
from examples.agents.views import templates
from examples.chat_load.origin import require_same_origin


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    async with Redis.from_url(store.REDIS_URL) as kv:
        await kv.get("agents:health")
        yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parents[2] / "static"), name="static")
app.mount("/assets", StaticFiles(directory=Path(__file__).parents[1] / "static"), name="assets")


async def connection() -> AsyncGenerator[Redis]:
    async with Redis.from_url(store.REDIS_URL) as kv:
        yield kv


type Store = Annotated[Redis, Depends(connection)]


async def require_turn(kv: Redis, session: UUID) -> Turn:
    turn = await store.read(kv, session.hex)
    if turn is None:
        raise HTTPException(404, "Unknown session")
    turn.stop_requested = bool(await kv.exists("agents:stop:" + session.hex))
    return turn


@app.get("/health")
async def health(kv: Store) -> dict[str, str]:
    await kv.get("agents:health")
    return {"status": "ready"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, kv: Store) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "usual": True,
            "node": "uvicorn",
            "sessions": await store.all_turns(kv),
        },
    )


@app.post("/sessions", dependencies=[Depends(require_same_origin)])
async def start(
    kv: Store,
    mode: Annotated[Scenario, Form()] = "normal",
    count: Annotated[int, Form(ge=1, le=12)] = 1,
) -> RedirectResponse:
    if (
        TypeAdapter(int).validate_python(await kv.execute_command("LLEN", "agents:sessions"))
        + count
        > 100
    ):
        raise HTTPException(503, "Demo session limit reached; restart the stack for a fresh run")
    for _ in range(count):
        turn = Turn(id=uuid4().hex, prompt=mode)
        await store.save(kv, turn)
        await kv.execute_command("RPUSH", "agents:sessions", turn.id)
        await asyncio.to_thread(queue.send_task, TURN, args=[turn.id], task_id=turn.id)
    return RedirectResponse("/", status_code=303)


@app.get("/api/sessions")
async def sessions(kv: Store) -> list[Turn]:
    return await store.all_turns(kv)


@app.get("/api/sessions/{session}")
async def inspect_session(session: UUID, kv: Store) -> View:
    return await store.view(kv, await require_turn(kv, session))


@app.get("/sessions/{session}", response_class=HTMLResponse)
async def detail(request: Request, session: UUID, kv: Store) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="session.html",
        context={
            "usual": True,
            "node": "uvicorn",
            "snapshot": await require_turn(kv, session),
        },
    )


@app.get("/sessions/{session}/events", response_class=EventSourceResponse)
async def events(session: UUID, kv: Store) -> AsyncGenerator[ServerSentEvent]:
    await require_turn(kv, session)
    async for fragment in store.fragments(kv, session.hex):
        yield ServerSentEvent(raw_data=fragment)


@app.post("/sessions/{session}/stop", dependencies=[Depends(require_same_origin)])
async def stop(session: UUID, kv: Store) -> RedirectResponse:
    turn = await require_turn(kv, session)
    if turn.state not in TERMINAL:
        await kv.set("agents:stop:" + session.hex, "1")
        result = queue.AsyncResult(session.hex)
        await asyncio.to_thread(result.revoke, terminate=True)
    return RedirectResponse("/sessions/" + session.hex, status_code=303)
