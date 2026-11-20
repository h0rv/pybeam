# HTMX shared counter

Use HTMX with Starlette to share a counter across browser tabs through PyBEAM state and pub/sub.

```sh
uv add python-beam starlette uvicorn
```

```python
# htmx_app.py
import asyncio
from collections.abc import AsyncIterator
from html import escape
from string.templatelib import Template

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response, StreamingResponse
from starlette.routing import Route
from uvicorn import Config, Server

from pybeam.integrations.hornbeam import Beam, run


def html(template: Template) -> str:
    out = (s if isinstance(s, str) else escape(str(s.value)) for s in template)
    return "".join(out)


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

    async def snapshots() -> AsyncIterator[str]:
        async with beam.subscribe("htmx:changed", max_bytes=4096) as changes:
            while True:
                count = await beam.incr("count", 0, namespace="htmx")
                yield html(t"data: {count}\n\n")
                await anext(changes)

    return StreamingResponse(snapshots(), media_type="text/event-stream")


app = Starlette(
    routes=[
        Route("/", index),
        Route("/increment", increment, methods=["POST"]),
        Route("/events", events),
    ]
)


async def main() -> None:
    server = Server(Config(app, port=8770, timeout_graceful_shutdown=2))
    try:
        await server.serve()
    except asyncio.CancelledError:
        if server.started:
            await server.shutdown()
        raise


if __name__ == "__main__":
    run(main)
```

```console
$ uv run htmx_app.py
```

Open <http://127.0.0.1:8770/> in two tabs and click Increment: both show the updated count.
Each SSE connection subscribes before reading a fresh snapshot, including on reconnect; the counter lives in memory until the node stops.
