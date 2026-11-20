# HTMX shared counter

Use HTMX with FastAPI to share a counter across browser tabs through PyBEAM state and pub/sub.

```sh
uv add python-beam fastapi uvicorn
```

```python
# htmx_app.py
import asyncio
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import EventSourceResponse, HTMLResponse
from uvicorn import Config, Server

from pybeam.integrations.fastapi.dependencies import Beam, provider
from pybeam.integrations.hornbeam import run

PAGE = """<!doctype html>
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
</html>"""

app = FastAPI(lifespan=provider.lifespan)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return PAGE


@app.post("/increment", status_code=204)
async def increment(beam: Beam) -> None:
    await beam.incr("count", namespace="htmx")
    await beam.publish("htmx:changed", b"count")


@app.get("/events", response_class=EventSourceResponse)
async def events(beam: Beam) -> AsyncIterator[int]:
    async with beam.subscribe("htmx:changed", max_bytes=4096) as changes:
        while True:
            yield await beam.incr("count", 0, namespace="htmx")
            await anext(changes)


async def main() -> None:
    server = Server(Config(app, port=8770, timeout_graceful_shutdown=1))
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
