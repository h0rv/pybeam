# FastAPI

Share an atomic counter across requests. The dependency opens one connection during the application lifespan and selects the request's scope.

```python
# fastapi_app.py
import asyncio

from fastapi import FastAPI
from uvicorn import Config, Server

from pybeam.integrations.fastapi.dependencies import Beam, provider
from pybeam.integrations.hornbeam import run

app = FastAPI(lifespan=provider.lifespan)


@app.post("/increment")
async def increment(beam: Beam) -> dict[str, int]:
    count = await beam.incr("visits", namespace="fastapi")
    return {"count": count}


@app.get("/counter")
async def counter(beam: Beam) -> dict[str, int]:
    count = await beam.incr("visits", 0, namespace="fastapi")
    return {"count": count}


async def main() -> None:
    config = Config(app, host="127.0.0.1", port=8767, loop="none", http="h11")
    server = Server(config)

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
$ uv run fastapi_app.py
```

The server listens on port 8767. In another terminal, increment the counter and read it:

```console
$ curl -X POST http://127.0.0.1:8767/increment
{"count":1}
$ curl http://127.0.0.1:8767/counter
{"count":1}
```

`incr` updates and returns the counter atomically, so concurrent requests do not need a lock. Values live in memory and are lost when the program stops. Stop the server with Ctrl+C.

The [HTMX example](htmx.md) adds an HTML form, partial responses and live updates through a subscription.
