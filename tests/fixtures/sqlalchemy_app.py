import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI

from examples.sqlalchemy.main import app
from examples.sqlalchemy.store import Base, Database, databases
from tests.native.sqlalchemy_pool import Connections

counters: dict[str, Connections] = {}


@asynccontextmanager
async def resources(*, asynchronous: bool = True) -> AsyncGenerator[None]:
    with TemporaryDirectory() as directory:
        db = Database(Path(directory))
        for mode, engine in (("sync", db.sync), ("async", db.asynchronous.sync_engine)):
            counter = Connections()
            counter.watch(engine)
            counters[mode] = counter
        try:
            if asynchronous:
                await db.start()
            else:
                await asyncio.to_thread(Base.metadata.create_all, db.sync)
            databases["notes"] = db
            yield
        finally:
            databases.clear()
            await db.close()
            assert all(counter.opened == counter.closed for counter in counters.values())
            print("http-pools-disposed", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    async with resources():
        yield


@asynccontextmanager
async def sync_lifespan(app: FastAPI) -> AsyncGenerator[None]:
    async with resources(asynchronous=False):
        yield


@app.get("/pool-stats")
async def pool_stats() -> dict[str, dict[str, int]]:
    return {
        mode: {"opened": count.opened, "checkouts": count.checkouts, "closed": count.closed}
        for mode, count in counters.items()
    }


app.router.lifespan_context = lifespan
