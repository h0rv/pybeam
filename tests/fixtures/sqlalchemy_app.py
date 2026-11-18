import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI

from examples.sqlalchemy.main import get_database, router
from examples.sqlalchemy.store import Base, Database, databases
from tests.native.sqlalchemy_pool import Connections

counters: dict[str, Connections] = {}


@asynccontextmanager
async def resources(*, asynchronous: bool = True) -> AsyncGenerator[Database]:
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
            yield db
        finally:
            databases.clear()
            await db.close()
            assert all(counter.opened == counter.closed for counter in counters.values())
            print("http-pools-disposed", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    async with resources() as db:

        async def get_test_database() -> Database:
            return db

        app.dependency_overrides[get_database] = get_test_database
        try:
            yield
        finally:
            app.dependency_overrides.clear()


@asynccontextmanager
async def sync_lifespan(app: FastAPI) -> AsyncGenerator[None]:
    async with resources(asynchronous=False) as db:

        async def get_test_database() -> Database:
            return db

        app.dependency_overrides[get_database] = get_test_database
        try:
            yield
        finally:
            app.dependency_overrides.clear()


async def pool_stats() -> dict[str, dict[str, int]]:
    return {
        mode: {"opened": count.opened, "checkouts": count.checkouts, "closed": count.closed}
        for mode, count in counters.items()
    }


app = FastAPI(lifespan=lifespan)
app.include_router(router)
app.get("/pool-stats")(pool_stats)
