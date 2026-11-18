import asyncio
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import APIRouter, FastAPI, Request, WebSocket
from fastapi.responses import PlainTextResponse


@dataclass(frozen=True, slots=True)
class Resources:
    loop: asyncio.AbstractEventLoop
    requests: asyncio.Queue[None]
    values: asyncio.Queue[str]

    async def feed(self) -> None:
        while True:
            await self.requests.get()
            await self.values.put("same-loop")

    async def consume(self) -> str:
        assert asyncio.get_running_loop() is self.loop, "Handler is not on the lifespan loop"
        async with asyncio.timeout(3):
            await self.requests.put(None)
            return await self.values.get()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[dict[str, object]]:
    resources = Resources(
        asyncio.get_running_loop(), asyncio.Queue(maxsize=1), asyncio.Queue(maxsize=1)
    )
    async with asyncio.TaskGroup() as group:
        worker = group.create_task(resources.feed())
        try:
            yield {"resource": resources}
        finally:
            worker.cancel()


def resource_from_scope(scope: Mapping[str, object]) -> Resources:
    state = scope.get("state")
    if not isinstance(state, dict):
        raise RuntimeError("Scope has no lifespan state")
    resource = state.get("resource")
    if not isinstance(resource, Resources):
        raise RuntimeError("Scope did not preserve the lifespan resource")
    assert "request_local" not in state, "Scope state was not shallow-copied"
    state["request_local"] = True
    return resource


router = APIRouter()


@router.get("/resource", response_class=PlainTextResponse)
async def resource(request: Request) -> str:
    return await resource_from_scope(request.scope).consume()


@router.websocket("/resource-ws")
async def resource_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_text(await resource_from_scope(websocket.scope).consume())
    await websocket.close()


app = FastAPI(lifespan=lifespan)
app.include_router(router)


@app.get("/health", response_class=PlainTextResponse)
async def health() -> str:
    return "ready"
