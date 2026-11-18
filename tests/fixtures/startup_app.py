from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from pybeam import ActorGroup
from pybeam.integrations.hornbeam import Beam
from tests.native.pythonic_actors import Counter


@asynccontextmanager
async def startup(_app: FastAPI) -> AsyncGenerator[None]:
    beam = await Beam.connect()
    async with ActorGroup() as group:
        counter = await group.spawn(Counter, value=17)
        assert await counter.read() == 17
        await beam.store("startup").set("ready", b"ready")
        yield


@asynccontextmanager
async def failed_startup(_app: FastAPI) -> AsyncGenerator[None]:
    raise RuntimeError("intentional lifespan startup failure")
    yield


app = FastAPI(lifespan=startup)
failed_app = FastAPI(lifespan=failed_startup)


@app.get("/health")
async def health() -> dict[str, bool]:
    beam = await Beam.connect()
    assert await beam.store("startup").get("ready") == b"ready"
    return {"ok": True}
