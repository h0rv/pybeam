from collections.abc import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from pybeam.integrations.fastapi.dependencies import Beam, provider

app = FastAPI(lifespan=provider.lifespan)


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/stats")
async def stats(beam: Beam) -> dict[str, int]:
    return {
        "subscriptions": await beam.subscription_count(),
        "finalized": await beam.incr("finalized", 0, namespace="sse-disconnect"),
    }


@app.post("/publish")
async def publish(beam: Beam) -> dict[str, int]:
    return {"targets": await beam.publish("sse-disconnect", b"invalidate")}


@app.get("/events", response_class=EventSourceResponse)
async def events(request: Request, beam: Beam) -> AsyncGenerator[ServerSentEvent]:
    services = (
        provider.for_scope(request.scope) if request.query_params.get("scoped") == "true" else beam
    )
    try:
        async with services.subscribe("sse-disconnect") as changes:
            yield ServerSentEvent(raw_data="ready")
            async for _message in changes:
                yield ServerSentEvent(raw_data="changed")
    finally:
        await beam.incr("finalized", namespace="sse-disconnect")
