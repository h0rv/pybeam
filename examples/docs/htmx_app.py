# htmx_app.py
import asyncio
from collections.abc import AsyncGenerator
from html import escape
from string.templatelib import Template

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response, StreamingResponse
from starlette.routing import Route
from uvicorn import Config, Server

from pybeam.integrations.hornbeam import Beam, run


def html(template: Template) -> str:
    parts: list[str] = []
    for part in template:
        value = part if isinstance(part, str) else escape(str(part.value))
        parts.append(value)
    return "".join(parts)


async def index(request: Request) -> HTMLResponse:
    return HTMLResponse(
        html(t"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shared counter</title>
<script src="https://unpkg.com/htmx.org@4.0.0/dist/htmx.min.js"></script>
<script src="https://unpkg.com/htmx.org@4.0.0/dist/ext/hx-sse.js"></script>
<main>
  <h1>Shared counter</h1>
  <p>Count: <output id="count" hx-sse:connect="/events"
     hx-config='{{"sse":{{"pauseOnBackground":false}}}}'
     hx-swap="innerHTML" aria-live="polite">Connecting…</output></p>
  <button hx-post="/increment" hx-swap="none">Increment</button>
</main>
</html>""")
    )


async def increment(request: Request) -> Response:
    beam = await Beam.connect()
    await beam.incr("count", namespace="htmx")
    await beam.publish("htmx:changed", b"count")
    return Response(status_code=204)


async def events(request: Request) -> StreamingResponse:
    beam = await Beam.connect()

    async def snapshots() -> AsyncGenerator[str]:
        async with beam.subscribe("htmx:changed", max_bytes=4096) as changes:
            while True:
                count = await beam.incr("count", 0, namespace="htmx")
                yield html(t"data: {count}\n\n")
                await anext(changes)

    stream = snapshots()
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
        background=BackgroundTask(stream.aclose),
    )


app = Starlette(
    routes=[
        Route("/", index),
        Route("/increment", increment, methods=["POST"]),
        Route("/events", events),
    ]
)


async def main() -> None:
    config = Config(app, host="127.0.0.1", port=8770, loop="none", http="h11")
    server = Server(config)
    try:
        await server.serve()
    except asyncio.CancelledError:
        if server.started:
            await server.shutdown()
        raise


if __name__ == "__main__":
    run(main)
