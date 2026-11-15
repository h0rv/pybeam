# Web integration

Use FastAPI handlers with BEAM services and an application-owned lifespan.

```python
import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from uvicorn import Config, Server

from pybeam.integrations.hornbeam.client import Beam
from pybeam.integrations.hornbeam.contracts import Services
from pybeam.integrations.hornbeam.runtime import run


@dataclass
class Resources:
    beam: Services | None = None


resources = Resources()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    resources.beam = await Beam.connect()
    try:
        yield
    finally:
        resources.beam = None


app = FastAPI(lifespan=lifespan)


async def services() -> Services:
    if resources.beam is None:
        raise HTTPException(503, "Services are not running")
    return resources.beam


type Native = Annotated[Services, Depends(services)]


@app.get("/counter")
async def read(beam: Native) -> dict[str, int]:
    return {"count": await beam.incr("visits", 0, namespace="web")}


@app.post("/increment")
async def increment(beam: Native) -> dict[str, int]:
    return {"count": await beam.incr("visits", 1, namespace="web")}


async def main() -> None:
    server = Server(
        Config(app, host="127.0.0.1", port=8767, loop="none", http="h11", lifespan="on")
    )
    try:
        await server.serve()
    except asyncio.CancelledError:
        if server.started:
            await server.shutdown()
        raise


if __name__ == "__main__":
    run(main)
```

Run this file with Python, then open `http://127.0.0.1:8767/docs`. Send `POST /increment`, then `GET /counter`. Both return `{"count": 1}` after the first increment in a fresh run. Stop the server with Ctrl+C.

The lifespan owns application access to Hornbeam services. Uvicorn serves HTTP inside the native event loop. For Hornbeam-served ASGI applications, the FastAPI provider adapter also uses Hornbeam scope extensions to supply connection-scoped access.

The [FastAPI counter](../../examples/fastapi/main.py) also demonstrates SSE. The [Litestar](../../examples/litestar/main.py) and [PyView](../../examples/pyview/main.py) examples adapt the same services to their frameworks.
