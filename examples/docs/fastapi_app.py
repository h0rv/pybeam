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
