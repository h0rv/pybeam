# Web integration

Share a counter across FastAPI requests.

```python
# web.py
import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from uvicorn import Config, Server

from pybeam.integrations.hornbeam import Beam, run
from pybeam.integrations.hornbeam.contracts import Services


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

```console
$ uv run web.py
```

```console
$ curl -X POST http://127.0.0.1:8767/increment
{"count":1}
$ curl http://127.0.0.1:8767/counter
{"count":1}
```

The counter is served on port 8767. In a fresh run, incrementing it once sets its value to `1`. Stop the server with Ctrl+C.

The lifespan connects once and every request reuses that connection. `loop="none"` tells Uvicorn to use the event loop PyBEAM already runs. Concurrent requests increment the counter atomically, without a lock.

The [FastAPI example](../../examples/fastapi/main.py) adds server-sent events. The [Litestar](../../examples/litestar/main.py) and [PyView](../../examples/pyview/main.py) examples use the same services.
