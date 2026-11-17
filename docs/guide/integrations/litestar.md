# Litestar

Use Litestar's store API to share values across requests. `BeamStore` keeps each store's keys in its own namespace.

```python
# docs_litestar.py
import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from litestar import Litestar, Request, get, post
from litestar.datastructures import State
from uvicorn import Config, Server

from pybeam.integrations.hornbeam import Beam, run
from pybeam.integrations.litestar.store import BeamStore


@asynccontextmanager
async def lifespan(app: Litestar) -> AsyncGenerator[None]:
    beam = await Beam.connect()
    app.stores.register("documents", BeamStore(beam, "litestar:documents"))
    await app.stores.get("documents").set("status", "draft")
    yield


@get("/status", media_type="text/plain")
async def status(request: Request[object, object, State]) -> str:
    value = await request.app.stores.get("documents").get("status")
    return value.decode("utf-8") if value is not None else ""


@post("/publish", status_code=200, media_type="text/plain")
async def publish(request: Request[object, object, State]) -> str:
    await request.app.stores.get("documents").set("status", "published")
    return "published"


app = Litestar(route_handlers=[status, publish], lifespan=[lifespan])


async def main() -> None:
    server = Server(Config(app, host="127.0.0.1", port=8768, loop="none", http="h11"))

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
$ uv run docs_litestar.py
```

The server listens on port 8768. In another terminal, read and update the document status:

```console
$ curl http://127.0.0.1:8768/status
draft
$ curl -X POST http://127.0.0.1:8768/publish
published
$ curl http://127.0.0.1:8768/status
published
```

The store accepts strings or bytes and returns bytes. Values live in memory and are lost when the program stops; this example also resets the status during startup. Stop the server with Ctrl+C.

The [full Litestar example](../../../examples/litestar/main.py) adds templates, an atomic counter and live updates.
