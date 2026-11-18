from collections.abc import AsyncGenerator

from fastapi import Depends, HTTPException, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from examples.docs.reconnect import app
from pybeam.integrations.fastapi.dependencies import Beam


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/subscriptions")
async def subscriptions(beam: Beam) -> dict[str, int]:
    return {"count": await beam.subscription_count()}


@app.get("/auth-attempts")
async def auth_attempts(beam: Beam) -> dict[str, int]:
    return {"count": await beam.incr("auth", 0, namespace="sse-reconnect")}


async def authorize(request: Request, beam: Beam) -> None:
    await beam.incr("auth", namespace="sse-reconnect")
    if request.query_params.get("token") != "valid":
        raise HTTPException(status_code=403)


@app.get("/events", response_class=EventSourceResponse, dependencies=[Depends(authorize)])
async def events(beam: Beam) -> AsyncGenerator[ServerSentEvent]:
    async with beam.subscribe("reconnect:changed") as changes:
        count = await beam.incr("visits", 0, namespace="reconnect")
        yield ServerSentEvent(raw_data=str(count), id=str(count))
        async for _message in changes:
            count = await beam.incr("visits", 0, namespace="reconnect")
            yield ServerSentEvent(raw_data=str(count), id=str(count))
            break
