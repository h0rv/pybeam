# litestar_app.py
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
    config = Config(app, host="127.0.0.1", port=8768, loop="none", http="h11")
    server = Server(config)

    try:
        await server.serve()
    except asyncio.CancelledError:
        if server.started:
            await server.shutdown()
        raise


if __name__ == "__main__":
    run(main)
